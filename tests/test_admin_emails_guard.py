"""The legacy /api/admin/emails guard must fail closed when ADMIN_KEY is unset."""
import os

import pytest
from fastapi.testclient import TestClient

# app.database insists on a URL at import time; nothing connects to it in these tests
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/none")

from app.main import app  # noqa: E402


@pytest.fixture
def client():
    # No lifespan context: the guard rejects before any database access
    return TestClient(app)


def test_unset_key_rejects_old_default_literal(client, monkeypatch):
    monkeypatch.delenv("ADMIN_KEY", raising=False)
    assert client.get("/api/admin/emails", params={"admin_key": "your_secret_admin_key"}).status_code == 403


def test_unset_key_rejects_missing_and_empty(client, monkeypatch):
    monkeypatch.delenv("ADMIN_KEY", raising=False)
    assert client.get("/api/admin/emails").status_code == 403
    assert client.get("/api/admin/emails", params={"admin_key": ""}).status_code == 403


def test_wrong_key_rejected(client, monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "right")
    assert client.get("/api/admin/emails", params={"admin_key": "wrong"}).status_code == 403
