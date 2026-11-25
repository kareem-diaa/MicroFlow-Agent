import json

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import text

from backend.database import SessionLocal
from backend.security import (
    decrypt_field,
    encrypt_field,
    hash_password,
    keyed_ref,
    verify_chain,
    verify_password,
)


def test_envelope_roundtrip_and_unique_ciphertexts():
    a = encrypt_field(125000.5, "invoice.amount")
    b = encrypt_field(125000.5, "invoice.amount")
    assert a != b  # fresh data key and nonce every time
    assert decrypt_field(a, "invoice.amount") == 125000.5
    assert "125000" not in a


def test_envelope_rejects_wrong_column_context():
    token = encrypt_field("Nile View Hotel", "invoice.buyer_name")
    with pytest.raises(ValueError):
        decrypt_field(token, "invoice.amount")


def test_envelope_detects_tampering():
    env = json.loads(encrypt_field(1000, "invoice.amount"))
    ct = bytearray(__import__("base64").b64decode(env["ct"]))
    ct[0] ^= 0x01
    env["ct"] = __import__("base64").b64encode(bytes(ct)).decode()
    with pytest.raises(InvalidTag):
        decrypt_field(json.dumps(env), "invoice.amount")


def test_password_hashing():
    h = hash_password("S3cret!")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("S3cret!", h)
    assert not verify_password("wrong", h)


def test_keyed_ref_is_stable_and_not_plaintext():
    assert keyed_ref("301-552-118") == keyed_ref(" 301-552-118 ")
    assert "301" not in keyed_ref("301-552-118")


def test_audit_chain_detects_direct_database_tampering(client, owner, loaded):
    db = SessionLocal()
    try:
        assert verify_chain(db)["valid"]
        # An attacker with raw DB access edits one row, bypassing the ORM guard.
        row_id = db.execute(text("SELECT id, action FROM audit_logs ORDER BY id LIMIT 1 OFFSET 2")).first()
        db.execute(text("UPDATE audit_logs SET action = 'tampered' WHERE id = :i"), {"i": row_id[0]})
        db.commit()
        result = verify_chain(db)
        assert result["valid"] is False
        assert result["broken_at_id"] == row_id[0]
        db.execute(text("UPDATE audit_logs SET action = :a WHERE id = :i"), {"a": row_id[1], "i": row_id[0]})
        db.commit()
        assert verify_chain(db)["valid"]
    finally:
        db.close()


def test_orm_blocks_audit_update():
    from backend.database import AuditLog

    db = SessionLocal()
    try:
        row = db.query(AuditLog).first()
        row.action = "edited"
        with pytest.raises(PermissionError):
            db.commit()
        db.rollback()
    finally:
        db.close()
