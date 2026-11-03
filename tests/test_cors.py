from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app


def test_audit_preflight_does_not_require_service_token(monkeypatch):
    monkeypatch.setattr(get_settings(), "SERVICE_TOKEN", "test-service-token")
    monkeypatch.setattr(get_settings(), "CORS_ALLOWED_ORIGINS", ["http://localhost:4200"])
    client = TestClient(create_app())
    response = client.options(
        "/api/audit_log/call-logs?limit=20&offset=0",
        headers={
            "Origin": "http://localhost:4200",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,x-audit-key,x-shipment-authorization",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:4200"

    response = client.get("/api/audit_log/call-logs", headers={"Origin": "http://localhost:4200"})
    assert response.status_code == 403
    assert response.headers["access-control-allow-origin"] == "http://localhost:4200"


def test_unlisted_origin_is_rejected(monkeypatch):
    monkeypatch.setattr(get_settings(), "CORS_ALLOWED_ORIGINS", ["http://localhost:4200"])
    response = TestClient(create_app()).options(
        "/api/audit_log/call-logs",
        headers={"Origin": "https://unlisted.example", "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
