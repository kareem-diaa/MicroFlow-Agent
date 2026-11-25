"""End-to-end API tests: the full copilot flow plus RBAC and tenant-isolation checks."""
from conftest import PASSWORD


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_unauthenticated_requests_rejected(client):
    assert client.post("/api/score-credit").status_code == 401
    assert client.get("/api/audit-logs", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401


def test_full_owner_flow(client, owner, loaded):
    assert sum(f.get("transactions_added", 0) for f in loaded["files"]) > 300
    score = client.post("/api/score-credit", headers=owner).json()
    assert 0 <= score["score"] <= 100
    assert len(score["breakdown"]) == 7
    assert abs(sum(b["contribution"] for b in score["breakdown"]) - score["score"]) < 0.5

    fc = client.post("/api/forecast-cashflow", headers=owner).json()
    assert fc["weeks"] == 13 and len(fc["scenarios"]["base"]) == 13
    assert any(a["type"] == "insolvency_risk" for a in fc["alerts"])

    run = client.post("/api/agent/run", headers=owner).json()
    assert [s["step"] for s in run["steps"]][:3] == ["observe", "analyse", "decide"]
    assert run["drafted_applications"], "agent should draft an application for the funding gap"
    app_id = run["drafted_applications"][0]["application_id"]

    # Re-running the agent does not create duplicate drafts.
    again = client.post("/api/agent/run", headers=owner).json()
    assert again["drafted_applications"][0]["reused"] is True

    approved = client.post(f"/api/financing/applications/{app_id}/approve", headers=owner).json()
    assert approved["status"] == "submitted_approved"
    assert approved["provider_response"]["reference"].startswith("SBX-")
    assert client.post(f"/api/financing/applications/{app_id}/approve", headers=owner).status_code == 409

    ans = client.post("/api/agent/ask", headers=owner, json={"question": "When do I run out of cash?"}).json()
    assert ans["intent"] == "runway"
    ar = client.post("/api/agent/ask", headers=owner, json={"question": "ليه التقييم بتاعي كده؟"}).json()
    assert ar["intent"] == "score"


def test_advisor_cannot_upload_or_approve(client, advisor, owner, loaded):
    r = client.post("/api/upload-statement", headers=advisor, files={"file": ("s.csv", b"date,description,debit,credit,balance\n")})
    assert r.status_code == 403
    client.post("/api/agent/run", headers=owner)
    apps = client.get("/api/financing/applications", headers=advisor).json()
    assert isinstance(apps, list)
    assert client.post("/api/financing/applications/1/approve", headers=advisor).status_code == 403


def test_auditor_is_read_only_and_sees_masked_amounts(client, auditor, owner, loaded):
    client.post("/api/score-credit", headers=owner)
    assert client.post("/api/score-credit", headers=auditor, params={"business_id": 1}).status_code == 403
    assert client.post("/api/demo/load-samples", headers=auditor).status_code == 403
    dossier = client.get("/api/credit-dossier", headers=auditor, params={"business_id": 1}).json()
    assert isinstance(dossier["metrics"]["annualised_turnover"], str)  # masked band
    assert client.get("/api/audit-logs/verify", headers=auditor).json()["valid"] is True


def test_tenant_isolation(client, other_owner, loaded):
    assert client.get("/api/invoices", headers=other_owner, params={"business_id": 1}).status_code == 403
    assert client.post("/api/agent/run", headers=other_owner, params={"business_id": 1}).status_code == 403
    logs = client.get("/api/audit-logs", headers=other_owner).json()
    assert all(entry["business_id"] in (None, 2) for entry in logs)


def test_malicious_upload_rejected(client, owner):
    evil = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><Invoice/>'
    assert client.post("/api/upload-invoice", headers=owner, files={"file": ("e.xml", evil)}).status_code == 422


def test_login_lockout(client):
    for _ in range(5):
        client.post("/api/auth/login", json={"email": "owner@delta.example", "password": "wrong"})
    r = client.post("/api/auth/login", json={"email": "owner@delta.example", "password": PASSWORD})
    assert r.status_code == 423
