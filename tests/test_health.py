"""Smoke tests for the health endpoint and basic app boot."""

from fastapi.testclient import TestClient

from mcpanel.main import app


def test_health_endpoint() -> None:
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_is_unauthenticated() -> None:
    client = TestClient(app)
    # No session cookie set; health must still respond.
    resp = client.get("/api/health")
    assert resp.status_code == 200
