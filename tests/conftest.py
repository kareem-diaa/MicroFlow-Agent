import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="microflow-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_tmp) / 'test.db'}"
os.environ.setdefault("MICROFLOW_MASTER_KEY", "dGVzdC1rZXktdGVzdC1rZXktdGVzdC1rZXktMTIzNDU=")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.main import app  # noqa: E402

PASSWORD = "Demo@2026!"


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


def login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="session")
def owner(client):
    return login(client, "owner@alnour.example")


@pytest.fixture(scope="session")
def advisor(client):
    return login(client, "advisor@microflow.example")


@pytest.fixture(scope="session")
def auditor(client):
    return login(client, "auditor@bank.example")


@pytest.fixture(scope="session")
def other_owner(client):
    return login(client, "owner@delta.example")


@pytest.fixture(scope="session")
def loaded(client, owner):
    r = client.post("/api/demo/load-samples", headers=owner)
    assert r.status_code == 200, r.text
    return r.json()
