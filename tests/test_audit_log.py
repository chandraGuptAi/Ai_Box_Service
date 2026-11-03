import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.modules.audit_log.client import VoiceAIAdminClient
from app.modules.audit_log.service import Service

AUDIT_KEY = "test-audit-key"
ADMIN_KEY = "test-admin-key"
RECORDING = "/api/audit_log/call-logs/{call_id}/recording"

# Shape copied from GET https://voiceai.bigship.direct/v1/admin/call-logs (synthetic values).
CALL = {
    "call_id": "50830db4374b4021a0ed84d536f63c48", "type": "campaign", "client_id": "C-1", "customer_name": "Test Buyer",
    "phone_number": "+910000000000", "status": "completed", "answered": True, "duration_s": 79, "cost_rupees": 12.0,
    "analysis": {"status": "done", "outcome": "callback"},
    "recording": {"available": True, "duration_s": 79, "format": "ogg/opus",
                  "url": "/v1/call-logs/50830db4374b4021a0ed84d536f63c48/recording",
                  "play_url": "/v1/recordings/50830db4374b4021a0ed84d536f63c48/audio?exp=1&sig=x",
                  "download_url": "/v1/recordings/50830db4374b4021a0ed84d536f63c48/audio?download=true&exp=1&sig=x",
                  "links_expire_at": "2026-11-03T07:28:49+00:00"},
}
PAGE = {"total": 1, "limit": 50, "offset": 0,
        "summary": {"calls": 1, "answered": 1, "not_answered": 0, "talk_time_s": 79, "cost_rupees": 12.0}, "calls": [CALL]}


@pytest.fixture
def make_client(monkeypatch):
    """Start the app with an audit key, then point the module at a fake Voice AI served by `handler`."""
    monkeypatch.setattr(get_settings(), "AUDIT_LOG_API_KEY", AUDIT_KEY)
    clients = []

    def factory(handler):
        app = create_app()
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        voice = VoiceAIAdminClient("https://voiceai.test", ADMIN_KEY, 5, transport=httpx.MockTransport(handler))
        app.state.audit_log_service = Service(voice, RECORDING)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def page_handler(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=PAGE)
    return handler


@pytest.mark.parametrize("headers", [{}, {"X-Audit-Key": "wrong"}])
def test_requires_the_audit_key(make_client, headers):
    seen = []
    res = make_client(page_handler(seen)).get("/api/audit_log/call-logs", headers=headers)
    assert res.status_code == 401
    assert seen == []  # Voice AI is never called


def test_disabled_without_a_configured_key(make_client, monkeypatch):
    client = make_client(page_handler([]))
    monkeypatch.setattr(get_settings(), "AUDIT_LOG_API_KEY", "")
    assert client.get("/api/audit_log/call-logs", headers={"X-Audit-Key": AUDIT_KEY}).status_code == 503


def test_lists_call_logs_with_admin_key_filters_and_own_recording_links(make_client):
    seen = []
    res = make_client(page_handler(seen)).get(
        "/api/audit_log/call-logs", headers={"X-Audit-Key": AUDIT_KEY},
        params={"client_id": "C-1", "status": "completed", "date_from": "2026-11-01", "date_to": "2026-11-03", "limit": 10})
    assert res.status_code == 200
    request = seen[0]
    assert request.url.path == "/v1/admin/call-logs"
    assert request.headers["x-admin-key"] == ADMIN_KEY and "x-audit-key" not in request.headers
    assert dict(request.url.params) == {"client_id": "C-1", "type": "all", "status": "completed", "date_from": "2026-11-01",
                                        "date_to": "2026-11-03", "limit": "10", "offset": "0"}
    body = res.json()
    assert body["summary"]["cost_rupees"] == 12.0 and body["total"] == 1
    recording = body["calls"][0]["recording"]
    own = "/api/audit_log/call-logs/50830db4374b4021a0ed84d536f63c48/recording"
    assert (recording["url"], recording["play_url"], recording["download_url"]) == (own, own, own + "?download=true")
    assert "links_expire_at" not in recording
    assert ADMIN_KEY not in res.text


def test_rejects_an_inverted_period_before_calling_voice_ai(make_client):
    seen = []
    res = make_client(page_handler(seen)).get("/api/audit_log/call-logs", headers={"X-Audit-Key": AUDIT_KEY},
                                              params={"date_from": "2026-11-03", "date_to": "2026-11-01"})
    assert res.status_code == 422 and seen == []


def test_call_detail_and_upstream_errors(make_client):
    def handler(request):
        if request.url.path.endswith("/missing"):
            return httpx.Response(404, json={"detail": "Call not found"})
        if request.url.path.endswith("/broken"):
            return httpx.Response(401, json={"detail": "bad key"})
        return httpx.Response(200, json=CALL)

    client = make_client(handler)
    headers = {"X-Audit-Key": AUDIT_KEY}
    ok = client.get(f"/api/audit_log/call-logs/{CALL['call_id']}", headers=headers)
    assert ok.status_code == 200 and ok.json()["recording"]["url"].startswith("/api/audit_log/")
    assert client.get("/api/audit_log/call-logs/missing", headers=headers).json() == {"detail": "Call not found"}
    rejected = client.get("/api/audit_log/call-logs/broken", headers=headers)
    assert rejected.status_code == 502 and "VOICEAI_ADMIN_KEY" in rejected.json()["detail"]
    assert client.get("/api/audit_log/call-logs/bad%2Fid", headers=headers).status_code in (404, 422)


def test_streams_the_recording_with_the_admin_key(make_client):
    seen = []

    def handler(request):
        seen.append(request)
        # A stream, as from the network (MockTransport pre-reads plain `content`, which a raw stream cannot replay).
        return httpx.Response(200, stream=httpx.ByteStream(b"OggS-audio"), headers={"content-type": "audio/ogg",
                              "content-disposition": 'attachment; filename="call.ogg"'})

    res = make_client(handler).get(f"/api/audit_log/call-logs/{CALL['call_id']}/recording",
                                   headers={"X-Audit-Key": AUDIT_KEY}, params={"download": "true"})
    assert res.status_code == 200 and res.content == b"OggS-audio"
    assert res.headers["content-type"] == "audio/ogg" and "call.ogg" in res.headers["content-disposition"]
    assert seen[0].url.path == f"/v1/admin/call-logs/{CALL['call_id']}/recording"
    assert seen[0].url.params["download"] == "true" and seen[0].headers["x-admin-key"] == ADMIN_KEY


def test_call_analyses_pass_the_outcome_filter(make_client):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"total": 0, "limit": 50, "offset": 0, "analyses": []})

    res = make_client(handler).get("/api/audit_log/call-analyses", headers={"X-Audit-Key": AUDIT_KEY},
                                   params={"outcome": "callback"})
    assert res.status_code == 200
    assert seen[0].url.path == "/v1/admin/call-analyses" and seen[0].url.params["outcome"] == "callback"
    assert "status" not in seen[0].url.params


@pytest.mark.parametrize("filters", [{}, {"client_id": "C-1", "status": "completed", "date_from": "2026-11-01"}])
def test_csv_export_all_pages_with_optional_filters(make_client, filters):
    import csv
    import io
    seen = []

    def handler(request):
        seen.append(request)
        offset = int(request.url.params["offset"])
        calls = [{**CALL, "call_id": str(offset), "customer_name": '=SUM(1,2)',
                  "extra": "Hindi: ??????, quoted \"text\""}] if offset == 0 else [{**CALL, "call_id": "second"}]
        return httpx.Response(200, json={"total": 2, "calls": calls})

    res = make_client(handler).get("/api/audit_log/call-logs/export/csv",
        headers={"X-Audit-Key": AUDIT_KEY}, params={**filters, "limit": 1, "offset": 99})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="call-logs.csv"' == res.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(res.content.decode("utf-8-sig"))))
    assert len(rows) == 2 and rows[1]["call_id"] == "second"
    assert rows[0]["customer_name"] == "'=SUM(1,2)"
    assert "??????" in rows[0]["extra"]
    assert [r.url.params["offset"] for r in seen] == ["0", "1"]
    assert all(r.url.params["limit"] == "500" for r in seen)
    assert all(all(r.url.params[k] == v for k, v in filters.items()) for r in seen)


def test_csv_export_empty_and_requires_auth(make_client):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"total": 0, "calls": []})
    client = make_client(handler)
    assert client.get("/api/audit_log/call-logs/export/csv").status_code == 401
    assert not seen
    res = client.get("/api/audit_log/call-logs/export/csv", headers={"X-Audit-Key": AUDIT_KEY})
    assert res.status_code == 200
    assert res.content.decode("utf-8-sig").startswith("call_id,")


def test_formatted_excel_export(make_client):
    import io
    from openpyxl import load_workbook
    res = make_client(page_handler([])).get("/api/audit_log/call-logs/export/xlsx", headers={"X-Audit-Key": AUDIT_KEY})
    assert res.status_code == 200
    assert "Bigship-Call-Report-" in res.headers["content-disposition"]
    sheet = load_workbook(io.BytesIO(res.content)).active
    assert sheet["A4"].value == "Call Id"
    assert sheet["A4"].fill.fgColor.rgb == "003346A8"
    assert sheet.freeze_panes == "D5"
    assert sheet["A5"].value == CALL["call_id"]
