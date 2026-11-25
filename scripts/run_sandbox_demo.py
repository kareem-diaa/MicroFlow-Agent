"""Execution evidence (submission deliverable 06).

Runs the complete MicroFlow AI scenario against a fresh sandbox database through the real
REST API and writes every request/response to evidence/:

  execution_log.jsonl   one JSON line per API call (timestamp, actor, method, path, status, response)
  security_tests.json   negative tests: RBAC denials, tenant isolation, tampering detection, XXE
  audit_chain.json      full hash-chained audit trail + verification result
  summary.json          key numbers for the deck and video

Usage:  python scripts/run_sandbox_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence"
EVIDENCE.mkdir(exist_ok=True)
_tmp = Path(tempfile.mkdtemp(prefix="microflow-sandbox-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp / 'sandbox.db'}"
sys.path.insert(0, str(ROOT))

import warnings  # noqa: E402

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from backend.database import SessionLocal  # noqa: E402
from backend.main import app  # noqa: E402
from backend.security import verify_chain  # noqa: E402

PASSWORD = os.getenv("MICROFLOW_DEMO_PASSWORD", "Demo@2026!")
log_file = (EVIDENCE / "execution_log.jsonl").open("w")


def record(actor: str, method: str, path: str, resp, note: str = "") -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    if isinstance(body, dict) and "access_token" in body:
        body = {**body, "access_token": body["access_token"][:18] + "...(redacted)"}
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "request": f"{method} {path}",
        "status": resp.status_code,
        "note": note,
        "response": body,
    }
    log_file.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
    return entry


def dump_database() -> None:
    """Database output dumps: rows exactly as stored (ciphertext) and the decrypted simulated records."""
    import csv

    from backend.security import decrypt_field

    db = SessionLocal()
    raw = {}
    for table, limit in [("businesses", 10), ("users", 10), ("invoices", 5), ("transactions", 5),
                         ("credit_scores", 2), ("financing_applications", 5), ("audit_logs", 5)]:
        rows = db.execute(text(f"SELECT * FROM {table} ORDER BY id LIMIT {limit}")).mappings().all()  # noqa: S608
        count = db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()  # noqa: S608
        cleaned = []
        for r in rows:
            r = dict(r)
            if "password_hash" in r:
                r["password_hash"] = r["password_hash"][:24] + "...(truncated)"
            for k, v in r.items():
                if isinstance(v, str) and v.startswith('{"v":'):
                    r[k] = json.loads(v)
            cleaned.append(r)
        raw[table] = {"total_rows": count, "sample_rows_as_stored": cleaned}
    (EVIDENCE / "db_dump_raw.json").write_text(json.dumps(raw, indent=2, default=str, ensure_ascii=False))

    txns = db.execute(text("SELECT id, business_id, txn_date, category, direction, description_enc, amount_enc, balance_enc "
                           "FROM transactions ORDER BY txn_date, id")).all()
    with (EVIDENCE / "simulated_transactions_decrypted.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "business_id", "date", "category", "direction", "description", "amount_egp", "balance_egp"])
        for t in txns:
            w.writerow([t.id, t.business_id, t.txn_date, t.category, t.direction,
                        decrypt_field(t.description_enc, "txn.description"), decrypt_field(t.amount_enc, "txn.amount"),
                        decrypt_field(t.balance_enc, "txn.balance") if t.balance_enc else ""])
    invs = db.execute(text("SELECT id, invoice_number, buyer_sector, buyer_name_enc, amount_enc, issue_date, due_date, "
                           "paid_date, status FROM invoices ORDER BY issue_date")).all()
    with (EVIDENCE / "simulated_invoices_decrypted.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "invoice_number", "buyer", "sector", "amount_egp", "issue_date", "due_date", "paid_date", "status"])
        for i in invs:
            w.writerow([i.id, i.invoice_number, decrypt_field(i.buyer_name_enc, "invoice.buyer_name"), i.buyer_sector,
                        decrypt_field(i.amount_enc, "invoice.amount"), i.issue_date, i.due_date, i.paid_date or "", i.status])
    apps = db.execute(text("SELECT id, status, created_by, payload_enc FROM financing_applications")).all()
    (EVIDENCE / "financing_applications_decrypted.json").write_text(json.dumps(
        [{"id": a.id, "status": a.status, "created_by": a.created_by,
          "payload": decrypt_field(a.payload_enc, "financing.payload")} for a in apps], indent=2, default=str))
    db.close()


def main() -> None:
    with TestClient(app) as c:
        tokens = {}
        for name, email in [
            ("owner", "owner@alnour.example"),
            ("advisor", "advisor@microflow.example"),
            ("auditor", "auditor@bank.example"),
            ("other_owner", "owner@delta.example"),
        ]:
            r = c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            record(name, "POST", "/api/auth/login", r)
            tokens[name] = {"Authorization": f"Bearer {r.json()['access_token']}"}
        O, A, U, X = tokens["owner"], tokens["advisor"], tokens["auditor"], tokens["other_owner"]

        # --- Happy path -------------------------------------------------------
        load = record("owner", "POST", "/api/demo/load-samples", c.post("/api/demo/load-samples", headers=O),
                      "Ingest 2 statements (CSV+PDF) and 3 invoice files (XML, JSON, PDF)")
        informal = (ROOT / "data" / "samples" / "informal_receipts.txt").read_bytes()
        record("owner", "POST", "/api/upload-statement (informal Arabic/English text)",
               c.post("/api/upload-statement", headers=O, files={"file": ("informal_receipts.txt", informal)}),
               "Fallback parser for informal records")
        score = record("owner", "POST", "/api/score-credit", c.post("/api/score-credit", headers=O))
        fc = record("owner", "POST", "/api/forecast-cashflow", c.post("/api/forecast-cashflow", headers=O))
        offers = record("owner", "GET", "/api/financing/offers", c.get("/api/financing/offers", headers=O))
        run = record("owner", "POST", "/api/agent/run", c.post("/api/agent/run", headers=O),
                     "Agent observes, analyses, decides and drafts financing (no auto-submit)")
        record("advisor", "GET", "/api/financing/applications", c.get("/api/financing/applications", headers=A))
        app_id = run["response"]["drafted_applications"][0]["application_id"]
        approve = record("owner", "POST", f"/api/financing/applications/{app_id}/approve",
                         c.post(f"/api/financing/applications/{app_id}/approve", headers=O),
                         "Owner approves; sandbox NBFI responds")
        for q in ["When do I run out of cash?", "Why is my credit score what it is?", "Who will pay late?",
                  "What financing should I take?", "متى تنفد السيولة؟"]:
            record("owner", "POST", "/api/agent/ask", c.post("/api/agent/ask", headers=O, json={"question": q}), q)
        record("auditor", "GET", "/api/credit-dossier?business_id=1",
               c.get("/api/credit-dossier", headers=U, params={"business_id": 1}), "Auditor sees masked amounts")
        record("owner", "GET", "/api/security/at-rest-sample", c.get("/api/security/at-rest-sample", headers=O),
               "Ciphertext as stored in the database")

        # --- Negative security tests -------------------------------------------
        tests = []

        def expect(name: str, actor: str, method: str, path: str, resp, expected: int):
            e = record(actor, method, path, resp, f"SECURITY TEST: {name}")
            tests.append({"test": name, "actor": actor, "request": f"{method} {path}",
                          "expected_status": expected, "actual_status": resp.status_code, "passed": resp.status_code == expected})

        expect("No token -> 401", "anonymous", "POST", "/api/score-credit", c.post("/api/score-credit"), 401)
        expect("Forged token -> 401", "anonymous", "GET", "/api/audit-logs",
               c.get("/api/audit-logs", headers={"Authorization": "Bearer eyJhbGciOiJub25lIn0.e30."}), 401)
        expect("Advisor cannot upload data", "advisor", "POST", "/api/upload-invoice",
               c.post("/api/upload-invoice", headers=A, files={"file": ("x.json", b"[]")}), 403)
        expect("Advisor cannot approve financing", "advisor", "POST", f"/api/financing/applications/{app_id}/approve",
               c.post(f"/api/financing/applications/{app_id}/approve", headers=A), 403)
        expect("Auditor cannot run credit scoring", "auditor", "POST", "/api/score-credit?business_id=1",
               c.post("/api/score-credit", headers=U, params={"business_id": 1}), 403)
        expect("Other tenant cannot read invoices", "other_owner", "GET", "/api/invoices?business_id=1",
               c.get("/api/invoices", headers=X, params={"business_id": 1}), 403)
        expect("Other tenant cannot run agent", "other_owner", "POST", "/api/agent/run?business_id=1",
               c.post("/api/agent/run", headers=X, params={"business_id": 1}), 403)
        expect("XXE / entity XML rejected", "owner", "POST", "/api/upload-invoice",
               c.post("/api/upload-invoice", headers=O, files={"file": ("evil.xml",
                      b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY e SYSTEM "file:///etc/passwd">]><Invoice>&e;</Invoice>')}), 422)
        expect("Oversized upload rejected", "owner", "POST", "/api/upload-statement",
               c.post("/api/upload-statement", headers=O, files={"file": ("big.csv", b"a" * (5 * 1024 * 1024 + 10))}), 413)
        expect("Double approval rejected", "owner", "POST", f"/api/financing/applications/{app_id}/approve",
               c.post(f"/api/financing/applications/{app_id}/approve", headers=O), 409)

        # Tamper test on the audit chain (raw SQL bypasses the ORM guard)
        db = SessionLocal()
        before = verify_chain(db)
        victim = db.execute(text("SELECT id, details FROM audit_logs WHERE action='ai.credit_score' LIMIT 1")).first()
        db.execute(text("UPDATE audit_logs SET details = :d WHERE id = :i"),
                   {"d": json.dumps({"score": 99.9}), "i": victim[0]})
        db.commit()
        tampered = verify_chain(db)
        db.execute(text("UPDATE audit_logs SET details = :d WHERE id = :i"), {"d": victim[1], "i": victim[0]})
        db.commit()
        restored = verify_chain(db)
        db.close()
        tests.append({"test": "Audit chain detects edited credit score", "before": before, "after_tamper": tampered,
                      "after_restore": restored, "passed": before["valid"] and not tampered["valid"] and restored["valid"]})

        for i in range(6):
            r = c.post("/api/auth/login", json={"email": "owner@delta.example", "password": f"guess{i}"})
        expect("Brute force -> account locked", "anonymous", "POST", "/api/auth/login (correct password after 5 failures)",
               c.post("/api/auth/login", json={"email": "owner@delta.example", "password": PASSWORD}), 423)

        verify = record("auditor", "GET", "/api/audit-logs/verify", c.get("/api/audit-logs/verify", headers=U))
        logs = c.get("/api/audit-logs", headers=U, params={"limit": 1000}).json()

    log_file.close()
    dump_database()
    (EVIDENCE / "security_tests.json").write_text(json.dumps(
        {"passed": sum(t["passed"] for t in tests), "total": len(tests), "tests": tests}, indent=2, default=str))
    (EVIDENCE / "audit_chain.json").write_text(json.dumps(
        {"verification": verify["response"], "entries": list(reversed(logs))}, indent=2, default=str, ensure_ascii=False))
    s, f, r = score["response"], fc["response"], run["response"]
    summary = {
        "business": "Al-Nour Bakery Supplies (synthetic)",
        "ingested": load["response"]["files"],
        "credit_score": s["score"], "grade": s["grade"],
        "score_breakdown": {b["factor"]: b["sub_score"] for b in s["breakdown"]},
        "start_balance": f["start_balance"], "min_balance_base": f["min_balance_base"],
        "min_balance_stress": f["min_balance_stress"], "funding_gap": f["funding_gap_base"],
        "alerts": [a["message"] for a in f["alerts"] if a["severity"] != "info"],
        "recommended_financing": {k: offers["response"]["recommended"][k] for k in ("provider", "product", "funds_unlocked", "total_cost", "cost_per_1000_egp")},
        "alternatives": [{k: o.get(k) for k in ("provider", "product", "eligible", "funds_unlocked", "total_cost", "reason")} for o in offers["response"]["options"]],
        "agent_steps": r["steps"],
        "provider_response": approve["response"].get("provider_response"),
        "security_tests_passed": f"{sum(t['passed'] for t in tests)}/{len(tests)}",
        "audit_entries": verify["response"]["entries_checked"], "audit_chain_valid": verify["response"]["valid"],
    }
    (EVIDENCE / "summary.json").write_text(json.dumps(summary, indent=2, default=str, ensure_ascii=False))
    print(json.dumps({k: summary[k] for k in ("credit_score", "grade", "funding_gap", "recommended_financing",
                                              "security_tests_passed", "audit_entries", "audit_chain_valid")}, indent=2))


if __name__ == "__main__":
    main()
