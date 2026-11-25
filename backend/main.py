"""MicroFlow AI REST API."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ai_engine, copilot
from .cache import cache
from .config import BASE_DIR, DEMO_PASSWORD, LOCKOUT_MINUTES, MAX_LOGIN_FAILURES, MAX_UPLOAD_BYTES, SEED_DEMO_USERS
from .database import (
    AuditLog,
    Business,
    CreditScore,
    FinancingApplication,
    Invoice,
    SessionLocal,
    Transaction,
    User,
    get_db,
    init_db,
)
from .parser import ParseError, parse_invoices, parse_statement
from .security import (
    ROLE_PERMISSIONS,
    allowed_business_ids,
    audit,
    create_access_token,
    decrypt_field,
    encrypt_field,
    get_current_user,
    hash_password,
    keyed_ref,
    mask_amount,
    require,
    resolve_business,
    sha256_bytes,
    verify_chain,
    verify_password,
)

SAMPLES_DIR = BASE_DIR / "data" / "samples"


# ---------------------------------------------------------------------------
# Startup: create tables, train/load model, seed demo tenants
# ---------------------------------------------------------------------------
def seed_demo(db: Session) -> None:
    if db.execute(select(User).limit(1)).scalar_one_or_none():
        return
    b1 = Business(
        name="Al-Nour Bakery Supplies",
        sector="food wholesale",
        city="Cairo",
        commercial_register_enc=encrypt_field("CR-CAI-118204", "business.commercial_register"),
        tax_id_enc=encrypt_field("512-884-301", "business.tax_id"),
    )
    b2 = Business(
        name="Delta Textiles Workshop",
        sector="textiles",
        city="Mahalla",
        commercial_register_enc=encrypt_field("CR-GHR-220931", "business.commercial_register"),
        tax_id_enc=encrypt_field("633-120-447", "business.tax_id"),
    )
    db.add_all([b1, b2])
    db.flush()
    pw = hash_password(DEMO_PASSWORD)
    db.add_all(
        [
            User(email="owner@alnour.example", full_name="Mona Hassan (owner)", role="sme_owner", password_hash=pw, business_id=b1.id),
            User(email="owner@delta.example", full_name="Tarek Fawzy (owner)", role="sme_owner", password_hash=pw, business_id=b2.id),
            User(email="advisor@microflow.example", full_name="Advisor (assigned to Al-Nour)", role="advisor", password_hash=pw, assigned_business_ids=[b1.id]),
            User(email="auditor@bank.example", full_name="Bank Auditor", role="auditor", password_hash=pw, assigned_business_ids=[b1.id, b2.id]),
        ]
    )
    db.commit()
    audit(db, "system.seed_demo_tenants", "system", "system", None, {"businesses": [b1.id, b2.id], "users": 4})


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    ai_engine.train_delay_model()
    if SEED_DEMO_USERS:
        with SessionLocal() as db:
            seed_demo(db)
    yield


app = FastAPI(
    title="MicroFlow Agent",
    version="1.0.0",
    description="MicroFlow Agent: cash-flow alerts, explainable alternative credit and financing matching for SMEs.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("MICROFLOW_CORS", "http://localhost:8501,http://127.0.0.1:8501").split(","),
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    business_id: int | None = None


# ---------------------------------------------------------------------------
# Data access helpers (decrypt on read, scoped to one business)
# ---------------------------------------------------------------------------
def load_invoices(db: Session, business_id: int) -> list[dict]:
    rows = db.execute(select(Invoice).where(Invoice.business_id == business_id).order_by(Invoice.issue_date)).scalars()
    return [
        {
            "id": r.id,
            "invoice_number": r.invoice_number,
            "buyer_ref": r.buyer_ref,
            "buyer_name": decrypt_field(r.buyer_name_enc, "invoice.buyer_name"),
            "buyer_sector": r.buyer_sector,
            "amount": decrypt_field(r.amount_enc, "invoice.amount"),
            "currency": r.currency,
            "issue_date": r.issue_date,
            "due_date": r.due_date,
            "paid_date": r.paid_date,
            "status": r.status,
        }
        for r in rows
    ]


def load_transactions(db: Session, business_id: int) -> list[dict]:
    rows = db.execute(select(Transaction).where(Transaction.business_id == business_id).order_by(Transaction.txn_date, Transaction.id)).scalars()
    return [
        {
            "txn_date": r.txn_date,
            "category": r.category,
            "direction": r.direction,
            "description": decrypt_field(r.description_enc, "txn.description"),
            "amount": decrypt_field(r.amount_enc, "txn.amount"),
            "balance": decrypt_field(r.balance_enc, "txn.balance") if r.balance_enc else None,
        }
        for r in rows
    ]


def analyse(db: Session, business_id: int) -> dict:
    """Run the full pipeline once and cache it until new data arrives."""
    cached = cache.get(f"analysis:{business_id}")
    if cached:
        return cached
    invoices = load_invoices(db, business_id)
    txns = load_transactions(db, business_id)
    if not txns:
        raise HTTPException(status.HTTP_409_CONFLICT, "Upload at least one bank statement first")
    open_preds = ai_engine.predict_delays(invoices)
    credit = ai_engine.score_business(txns, invoices, open_preds)
    as_of = max(t["txn_date"] for t in txns) + timedelta(days=1)
    forecast = copilot.forecast_cashflow(
        txns, open_preds, credit["metrics"]["latest_balance"], as_of, credit["metrics"]["historical_dso_days"]
    )
    financing = copilot.match_financing(
        open_preds, credit["score"], forecast["funding_gap_base"], credit["metrics"]["avg_monthly_inflow"], as_of
    )
    result = {"invoices": invoices, "open": open_preds, "credit": credit, "forecast": forecast, "financing": financing, "as_of": as_of}
    cache.set(f"analysis:{business_id}", result)
    return result


def _json_safe(obj):
    return json.loads(json.dumps(obj, default=str))


def _actor(user: User) -> str:
    return f"user:{user.id}:{user.email}"


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"File exceeds {MAX_UPLOAD_BYTES} bytes")
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty file")
    return data


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"status": "ok", "model": ai_engine.model_metrics()["version"], "time": datetime.now(timezone.utc).isoformat()}


@app.post("/api/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.execute(select(User).where(User.email == body.email.lower())).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if user and user.locked_until and user.locked_until.replace(tzinfo=timezone.utc) > now:
        audit(db, "auth.login_blocked_locked", body.email, "anonymous", None, {})
        raise HTTPException(status.HTTP_423_LOCKED, "Account temporarily locked")
    if not user or not verify_password(body.password, user.password_hash):
        if user:
            user.failed_logins = (user.failed_logins or 0) + 1
            if user.failed_logins >= MAX_LOGIN_FAILURES:
                user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                user.failed_logins = 0
            db.commit()
        audit(db, "auth.login_failed", body.email, "anonymous", None, {})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    user.failed_logins = 0
    user.locked_until = None
    db.commit()
    audit(db, "auth.login_success", _actor(user), user.role, user.business_id, {})
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "role": user.role,
        "permissions": sorted(ROLE_PERMISSIONS[user.role]),
        "business_ids": sorted(allowed_business_ids(user)),
    }


@app.get("/api/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    businesses = [
        {"id": b.id, "name": b.name, "sector": b.sector, "city": b.city}
        for b in db.execute(select(Business).where(Business.id.in_(allowed_business_ids(user)))).scalars()
    ]
    return {"id": user.id, "email": user.email, "name": user.full_name, "role": user.role, "businesses": businesses,
            "permissions": sorted(ROLE_PERMISSIONS[user.role])}


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
def store_invoices(db: Session, user: User, business_id: int, filename: str, data: bytes, source: str) -> dict:
    try:
        parsed = parse_invoices(filename, data)
    except ParseError as exc:
        audit(db, "ingest.invoice_rejected", _actor(user), user.role, business_id, {"file": filename, "error": str(exc)})
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    digest = sha256_bytes(data)
    existing = set(db.execute(select(Invoice.invoice_number).where(Invoice.business_id == business_id)).scalars())
    added, skipped = [], []
    for inv in parsed:
        if inv["invoice_number"] in existing:
            skipped.append(inv["invoice_number"])
            continue
        db.add(
            Invoice(
                business_id=business_id,
                invoice_number=inv["invoice_number"],
                buyer_ref=keyed_ref(inv["buyer_tax_id"]),
                buyer_name_enc=encrypt_field(inv["buyer_name"], "invoice.buyer_name"),
                buyer_tax_id_enc=encrypt_field(inv["buyer_tax_id"], "invoice.buyer_tax_id"),
                buyer_sector=inv["buyer_sector"],
                amount_enc=encrypt_field(inv["amount"], "invoice.amount"),
                currency=inv["currency"],
                issue_date=inv["issue_date"],
                due_date=inv["due_date"],
                paid_date=inv["paid_date"],
                status="paid" if inv["paid_date"] else "open",
                source=source,
                source_sha256=digest,
            )
        )
        existing.add(inv["invoice_number"])
        added.append(inv["invoice_number"])
    db.commit()
    cache.delete(f"analysis:{business_id}")
    audit(db, "ingest.invoices", _actor(user), user.role, business_id,
          {"file": filename, "sha256": digest, "added": len(added), "duplicates_skipped": skipped})
    return {"file": filename, "sha256": digest, "invoices_added": len(added), "duplicates_skipped": skipped}


def store_statement(db: Session, user: User, business_id: int, filename: str, data: bytes) -> dict:
    try:
        rows = parse_statement(filename, data)
    except ParseError as exc:
        audit(db, "ingest.statement_rejected", _actor(user), user.role, business_id, {"file": filename, "error": str(exc)})
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    digest = sha256_bytes(data)
    if db.execute(select(Transaction.id).where(Transaction.business_id == business_id, Transaction.source_sha256 == digest).limit(1)).first():
        return {"file": filename, "sha256": digest, "transactions_added": 0, "note": "File already ingested"}
    for r in rows:
        db.add(
            Transaction(
                business_id=business_id,
                txn_date=r["txn_date"],
                category=r["category"],
                direction=r["direction"],
                description_enc=encrypt_field(r["description"], "txn.description"),
                amount_enc=encrypt_field(r["amount"], "txn.amount"),
                balance_enc=encrypt_field(r["balance"], "txn.balance") if r["balance"] is not None else None,
                source_sha256=digest,
            )
        )
    db.commit()
    cache.delete(f"analysis:{business_id}")
    cats: dict[str, int] = {}
    for r in rows:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    audit(db, "ingest.statement", _actor(user), user.role, business_id, {"file": filename, "sha256": digest, "rows": len(rows), "categories": cats})
    return {"file": filename, "sha256": digest, "transactions_added": len(rows), "categories": cats}


@app.post("/api/upload-invoice")
async def upload_invoice(
    file: UploadFile = File(...),
    business_id: int | None = Query(None),
    user: User = Depends(require("data:upload")),
    db: Session = Depends(get_db),
):
    bid = resolve_business(user, business_id)
    data = await _read_upload(file)
    return store_invoices(db, user, bid, file.filename or "upload", data, "upload")


@app.post("/api/upload-statement")
async def upload_statement(
    file: UploadFile = File(...),
    business_id: int | None = Query(None),
    user: User = Depends(require("data:upload")),
    db: Session = Depends(get_db),
):
    bid = resolve_business(user, business_id)
    data = await _read_upload(file)
    return store_statement(db, user, bid, file.filename or "upload", data)


@app.post("/api/demo/load-samples")
def load_samples(user: User = Depends(require("data:upload")), db: Session = Depends(get_db)):
    """Loads the bundled synthetic files for the caller's own business (demo convenience)."""
    bid = resolve_business(user, None)
    if not SAMPLES_DIR.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run scripts/generate_samples.py first")
    results = []
    for path in sorted(SAMPLES_DIR.glob("statement_*")):
        results.append(store_statement(db, user, bid, path.name, path.read_bytes()))
    for path in sorted(SAMPLES_DIR.glob("invoices_*")) + sorted(SAMPLES_DIR.glob("invoice_*.pdf")):
        results.append(store_invoices(db, user, bid, path.name, path.read_bytes(), "sample"))
    return {"business_id": bid, "files": results}


@app.get("/api/invoices")
def list_invoices(business_id: int | None = Query(None), user: User = Depends(require("data:read")), db: Session = Depends(get_db)):
    bid = resolve_business(user, business_id)
    invoices = load_invoices(db, bid)
    try:
        preds = {p["id"]: p for p in analyse(db, bid)["open"]}
    except HTTPException:
        preds = {}
    out = []
    for inv in invoices:
        p = preds.get(inv["id"], {})
        out.append(
            {
                **{k: v for k, v in inv.items() if k != "buyer_ref"},
                "predicted_delay_days": p.get("predicted_delay_days"),
                "expected_collection_date": p.get("expected_collection_date"),
            }
        )
    return _json_safe(out)


# ---------------------------------------------------------------------------
# AI: credit, forecast, financing, agent, Q&A
# ---------------------------------------------------------------------------
@app.post("/api/score-credit")
def score_credit(business_id: int | None = Query(None), user: User = Depends(require("credit:score")), db: Session = Depends(get_db)):
    bid = resolve_business(user, business_id)
    result = analyse(db, bid)
    credit = _json_safe(result["credit"])
    dossier_hash = hashlib.sha256(json.dumps(credit, sort_keys=True).encode()).hexdigest()
    db.add(CreditScore(business_id=bid, score=credit["score"], grade=credit["grade"],
                       dossier_enc=encrypt_field(credit, "credit.dossier"), model_version=credit["model_version"]))
    db.commit()
    audit(db, "ai.credit_score", _actor(user), user.role, bid,
          {"score": credit["score"], "grade": credit["grade"], "model_version": credit["model_version"], "dossier_sha256": dossier_hash})
    return {**credit, "dossier_sha256": dossier_hash}


@app.get("/api/credit-dossier")
def credit_dossier(business_id: int | None = Query(None), user: User = Depends(require("credit:read")), db: Session = Depends(get_db)):
    bid = resolve_business(user, business_id)
    row = db.execute(select(CreditScore).where(CreditScore.business_id == bid).order_by(CreditScore.id.desc()).limit(1)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No dossier yet; run /api/score-credit")
    dossier = decrypt_field(row.dossier_enc, "credit.dossier")
    if user.role == "auditor":
        dossier["metrics"] = {k: mask_amount(v, "auditor") if isinstance(v, (int, float)) and v > 1000 else v for k, v in dossier["metrics"].items()}
    audit(db, "credit.dossier_viewed", _actor(user), user.role, bid, {"credit_score_id": row.id})
    business = db.get(Business, bid)
    return {"business": business.name, "generated_at": row.created_at, **dossier}


@app.post("/api/forecast-cashflow")
def forecast(business_id: int | None = Query(None), user: User = Depends(require("forecast:run")), db: Session = Depends(get_db)):
    bid = resolve_business(user, business_id)
    result = analyse(db, bid)
    fc = _json_safe(result["forecast"])
    audit(db, "ai.cashflow_forecast", _actor(user), user.role, bid,
          {"funding_gap": fc["funding_gap_base"], "min_base": fc["min_balance_base"], "alerts": [a["type"] for a in fc["alerts"]]})
    return fc


@app.get("/api/financing/offers")
def financing_offers(business_id: int | None = Query(None), user: User = Depends(require("financing:read")), db: Session = Depends(get_db)):
    bid = resolve_business(user, business_id)
    return _json_safe(analyse(db, bid)["financing"])


@app.post("/api/agent/run")
def agent_run(business_id: int | None = Query(None), user: User = Depends(require("agent:run")), db: Session = Depends(get_db)):
    bid = resolve_business(user, business_id)
    result = analyse(db, bid)
    plan = copilot.agent_plan(result["forecast"], result["credit"], result["financing"])
    drafted = []
    for action in plan["actions"]:
        if action["type"] != "draft_financing_application":
            continue
        offer = action["offer"]
        invoice_ids = sorted(i["invoice_id"] for i in offer.get("invoices", []))
        existing = db.execute(
            select(FinancingApplication).where(
                FinancingApplication.business_id == bid,
                FinancingApplication.offer_id == offer["offer_id"],
                FinancingApplication.status == "pending_owner_approval",
            )
        ).scalars().all()
        dup = next((a for a in existing if sorted(a.invoice_ids or []) == invoice_ids), None)
        if dup:
            drafted.append({"application_id": dup.id, "status": dup.status, "reused": True})
            continue
        payload = _json_safe({**offer, "credit_score": result["credit"]["score"], "funding_gap": result["forecast"]["funding_gap_base"],
                              "as_of": result["as_of"]})
        app_row = FinancingApplication(
            business_id=bid, offer_id=offer["offer_id"], provider=offer["provider"], product=offer["product"],
            invoice_ids=invoice_ids, payload_enc=encrypt_field(payload, "financing.payload"),
            status="pending_owner_approval", created_by="agent",
        )
        db.add(app_row)
        db.commit()
        drafted.append({"application_id": app_row.id, "status": app_row.status, "reused": False})
        audit(db, "agent.financing_drafted", "agent:microflow", "agent", bid,
              {"application_id": app_row.id, "offer_id": offer["offer_id"], "invoice_ids": invoice_ids,
               "funds": offer["funds_unlocked"], "cost": offer["total_cost"], "triggered_by": _actor(user)})
    decision_hash = hashlib.sha256(json.dumps(_json_safe(plan), sort_keys=True).encode()).hexdigest()
    audit(db, "agent.run", _actor(user), user.role, bid,
          {"steps": [s["step"] for s in plan["steps"]], "actions": [a["type"] for a in plan["actions"]], "decision_sha256": decision_hash})
    return _json_safe({**plan, "alerts": result["forecast"]["alerts"], "drafted_applications": drafted, "decision_sha256": decision_hash})


@app.get("/api/financing/applications")
def list_applications(business_id: int | None = Query(None), user: User = Depends(require("financing:read")), db: Session = Depends(get_db)):
    bid = resolve_business(user, business_id)
    rows = db.execute(select(FinancingApplication).where(FinancingApplication.business_id == bid).order_by(FinancingApplication.id.desc())).scalars()
    out = []
    for r in rows:
        payload = decrypt_field(r.payload_enc, "financing.payload")
        out.append({
            "id": r.id, "provider": r.provider, "product": r.product, "status": r.status, "created_by": r.created_by,
            "invoice_ids": r.invoice_ids, "funds_unlocked": mask_amount(payload["funds_unlocked"], user.role),
            "total_cost": mask_amount(payload["total_cost"], user.role), "provider_response": payload.get("provider_response"),
            "created_at": r.created_at,
        })
    return _json_safe(out)


def _decide(application_id: int, approve: bool, user: User, db: Session) -> dict:
    row = db.get(FinancingApplication, application_id)
    if row is None or row.business_id not in allowed_business_ids(user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    if row.status != "pending_owner_approval":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Application is already {row.status}")
    payload = decrypt_field(row.payload_enc, "financing.payload")
    row.decided_at = datetime.now(timezone.utc)
    row.approved_by = user.id
    if not approve:
        row.status = "rejected_by_owner"
        db.commit()
        audit(db, "financing.owner_rejected", _actor(user), user.role, row.business_id, {"application_id": row.id})
        return {"application_id": row.id, "status": row.status}
    response = copilot.sandbox_provider_decision(payload, payload["credit_score"])
    payload["provider_response"] = response
    row.payload_enc = encrypt_field(payload, "financing.payload")
    row.status = "submitted_" + response["decision"]
    if response["decision"] == "approved":
        for inv in db.execute(select(Invoice).where(Invoice.id.in_(row.invoice_ids or []))).scalars():
            inv.status = "financed"
    db.commit()
    cache.delete(f"analysis:{row.business_id}")
    audit(db, "financing.owner_approved_and_submitted", _actor(user), user.role, row.business_id,
          {"application_id": row.id, "provider_reference": response["reference"], "decision": response["decision"]})
    return {"application_id": row.id, "status": row.status, "provider_response": response}


@app.post("/api/financing/applications/{application_id}/approve")
def approve_application(application_id: int, user: User = Depends(require("financing:approve")), db: Session = Depends(get_db)):
    return _decide(application_id, True, user, db)


@app.post("/api/financing/applications/{application_id}/reject")
def reject_application(application_id: int, user: User = Depends(require("financing:approve")), db: Session = Depends(get_db)):
    return _decide(application_id, False, user, db)


@app.post("/api/agent/ask")
def agent_ask(body: AskIn, user: User = Depends(require("agent:ask")), db: Session = Depends(get_db)):
    bid = resolve_business(user, body.business_id)
    r = analyse(db, bid)
    ans = copilot.answer_question(body.question, r["forecast"], r["credit"], r["financing"], r["open"])
    audit(db, "agent.question", _actor(user), user.role, bid, {"intent": ans["intent"], "question_sha256": hashlib.sha256(body.question.encode()).hexdigest()})
    return ans


@app.get("/api/model/metrics")
def metrics(user: User = Depends(get_current_user)):  # noqa: ARG001
    return ai_engine.model_metrics()


# ---------------------------------------------------------------------------
# Audit vault
# ---------------------------------------------------------------------------
@app.get("/api/audit-logs")
def audit_logs(limit: int = Query(200, le=1000), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    perms = ROLE_PERMISSIONS[user.role]
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if "audit:read_all" not in perms:
        if "audit:read_own" not in perms:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "No audit access")
        q = q.where(AuditLog.business_id.in_(allowed_business_ids(user)))
    rows = db.execute(q).scalars().all()
    return [
        {"id": r.id, "ts": r.ts, "actor": r.actor, "role": r.role, "action": r.action, "business_id": r.business_id,
         "details": r.details, "prev_hash": r.prev_hash, "entry_hash": r.entry_hash}
        for r in rows
    ]


@app.get("/api/audit-logs/verify")
def audit_verify(user: User = Depends(require("audit:verify")), db: Session = Depends(get_db)):
    result = verify_chain(db)
    audit(db, "audit.chain_verified", _actor(user), user.role, None, result)
    return result


@app.get("/api/security/at-rest-sample")
def at_rest_sample(business_id: int | None = Query(None), user: User = Depends(require("credit:read")), db: Session = Depends(get_db)):
    """Shows exactly what is stored in the database for one invoice (ciphertext, not plaintext)."""
    bid = resolve_business(user, business_id)
    row = db.execute(select(Invoice).where(Invoice.business_id == bid).limit(1)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No invoices stored")
    return {"invoice_number": row.invoice_number, "buyer_ref (HMAC)": row.buyer_ref,
            "buyer_name_enc": json.loads(row.buyer_name_enc), "amount_enc": json.loads(row.amount_enc)}
