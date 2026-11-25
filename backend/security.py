"""Security controls: envelope encryption, password hashing, JWT, RBAC and the hash-chained audit log."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import JWT_ALGORITHM, JWT_SECRET, JWT_TTL_MINUTES, MASTER_KEY
from .database import AuditLog, User, get_db

# ----------------------------------------------------------------------------
# 1. Envelope encryption (AES-256-GCM)
#    Each value gets a fresh 256-bit data key (DEK). The DEK encrypts the value;
#    the master key (KEK) wraps the DEK. Only the wrapped DEK is stored, so the
#    KEK can live in a KMS/HSM and be rotated by re-wrapping DEKs only.
#    The field context (e.g. "invoice.amount") is bound as AAD, so a ciphertext
#    copied into another column fails authentication.
# ----------------------------------------------------------------------------
_ENVELOPE_VERSION = 1


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


def encrypt_field(value: Any, context: str) -> str:
    plaintext = json.dumps(value, separators=(",", ":"), default=str).encode()
    aad = context.encode()
    dek = AESGCM.generate_key(bit_length=256)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
    wrap_nonce = secrets.token_bytes(12)
    wrapped_dek = AESGCM(MASTER_KEY).encrypt(wrap_nonce, dek, b"dek-wrap:" + aad)
    return json.dumps(
        {
            "v": _ENVELOPE_VERSION,
            "alg": "AES-256-GCM",
            "ctx": context,
            "wk": _b64(wrapped_dek),
            "wn": _b64(wrap_nonce),
            "n": _b64(nonce),
            "ct": _b64(ciphertext),
        },
        separators=(",", ":"),
    )


def decrypt_field(token: str, context: str) -> Any:
    env = json.loads(token)
    if env.get("v") != _ENVELOPE_VERSION or env.get("ctx") != context:
        raise ValueError("Envelope context mismatch")
    aad = context.encode()
    dek = AESGCM(MASTER_KEY).decrypt(_unb64(env["wn"]), _unb64(env["wk"]), b"dek-wrap:" + aad)
    plaintext = AESGCM(dek).decrypt(_unb64(env["n"]), _unb64(env["ct"]), aad)
    return json.loads(plaintext)


def keyed_ref(value: str) -> str:
    """Deterministic, non-reversible reference (HMAC-SHA256) used to group by buyer without exposing the tax id."""
    return hmac.new(MASTER_KEY, value.strip().upper().encode(), hashlib.sha256).hexdigest()[:32]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------------------
# 2. Passwords (PBKDF2-HMAC-SHA256, 600k iterations, per-user salt)
# ----------------------------------------------------------------------------
_PBKDF2_ITERS = 600_000  # OWASP Password Storage Cheat Sheet recommendation for PBKDF2-HMAC-SHA256


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, dk = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(iters))
    return hmac.compare_digest(candidate, _unb64(dk))


# ----------------------------------------------------------------------------
# 3. JWT authentication
# ----------------------------------------------------------------------------
_bearer = HTTPBearer(auto_error=False)


def create_access_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=JWT_TTL_MINUTES)).timestamp()),
        "jti": secrets.token_hex(8),
        "iss": "microflow-ai",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    try:
        payload = jwt.decode(
            creds.credentials,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            issuer="microflow-ai",
            options={"require": ["exp", "sub", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token") from exc
    user = db.get(User, int(payload["sub"]))
    if user is None or not user.is_active or user.role != payload.get("role"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or role changed")
    return user


# ----------------------------------------------------------------------------
# 4. RBAC: permission tiers + tenant scoping
# ----------------------------------------------------------------------------
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "sme_owner": {
        "data:upload", "data:read", "credit:score", "credit:read", "forecast:run",
        "agent:run", "agent:ask", "financing:read", "financing:approve", "audit:read_own",
    },
    "advisor": {
        "data:read", "credit:score", "credit:read", "forecast:run", "agent:run",
        "agent:ask", "financing:read", "audit:read_own",
    },
    "auditor": {"credit:read", "financing:read", "audit:read_all", "audit:verify"},
}


def require(permission: str):
    def checker(user: User = Depends(get_current_user)) -> User:
        if permission not in ROLE_PERMISSIONS.get(user.role, set()):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Role '{user.role}' lacks '{permission}'")
        return user

    return checker


def allowed_business_ids(user: User) -> set[int]:
    if user.role == "sme_owner":
        return {user.business_id} if user.business_id else set()
    return set(user.assigned_business_ids or [])


def resolve_business(user: User, business_id: int | None) -> int:
    """Return the business the user may act on, enforcing tenant isolation."""
    allowed = allowed_business_ids(user)
    if business_id is None:
        if len(allowed) == 1:
            return next(iter(allowed))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "business_id is required for this role")
    if business_id not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No access to this business")
    return business_id


def mask_amount(amount: float, role: str) -> float | str:
    """Auditors see magnitudes, not exact figures (data minimisation)."""
    if role == "auditor":
        if amount < 10_000:
            return "<10k"
        if amount < 100_000:
            return "10k-100k"
        if amount < 1_000_000:
            return "100k-1M"
        return ">1M"
    return round(amount, 2)


# ----------------------------------------------------------------------------
# 5. Hash-chained audit log
#    entry_hash = SHA256(prev_hash || canonical_json(entry)). Any edit, deletion
#    or reordering breaks every later hash, which verify_chain() detects.
# ----------------------------------------------------------------------------
GENESIS_HASH = "0" * 64


def _canonical(entry: dict) -> bytes:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str).encode()


def _entry_dict(ts: str, actor: str, role: str, action: str, business_id: int | None, details: dict) -> dict:
    return {
        "ts": ts,
        "actor": actor,
        "role": role,
        "action": action,
        "business_id": business_id,
        "details": details,
    }


def audit(
    db: Session,
    action: str,
    actor: str,
    role: str,
    business_id: int | None = None,
    details: dict | None = None,
) -> AuditLog:
    details = json.loads(json.dumps(details or {}, default=str))
    last = db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(1)).scalar_one_or_none()
    prev_hash = last.entry_hash if last else GENESIS_HASH
    ts = datetime.now(timezone.utc).isoformat()
    body = _entry_dict(ts, actor, role, action, business_id, details)
    entry_hash = hashlib.sha256(prev_hash.encode() + _canonical(body)).hexdigest()
    row = AuditLog(
        ts=ts,
        actor=actor,
        role=role,
        action=action,
        business_id=business_id,
        details=details,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )
    db.add(row)
    db.commit()
    return row


def verify_chain(db: Session) -> dict:
    rows = db.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()
    prev = GENESIS_HASH
    for row in rows:
        body = _entry_dict(row.ts, row.actor, row.role, row.action, row.business_id, row.details or {})
        expected = hashlib.sha256(prev.encode() + _canonical(body)).hexdigest()
        if row.prev_hash != prev or row.entry_hash != expected:
            return {"valid": False, "entries_checked": len(rows), "broken_at_id": row.id}
        prev = row.entry_hash
    return {"valid": True, "entries_checked": len(rows), "head_hash": prev}
