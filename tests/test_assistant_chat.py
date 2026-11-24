"""AI Assistant chat: saved conversations, streamed answers, token usage and day/week/month limits."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.ai_assistant import module as assistant_module
from app.modules.ai_assistant.chat import history_messages
from app.modules.ai_assistant.limits import IST, windows
from app.modules.ai_assistant.store import MemoryChatStore

USER = {"X-User-Id": "102795"}
OTHER = {"X-User-Id": "555"}
EMAIL = "Subject: Order delayed\n\nDear [customer name],\nSorry for the delay.\n\nRegards,\n[shop name]"


def sse(pieces, usage=None, finish="stop"):
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': p}, 'finish_reason': None}]})}" for p in pieces]
    lines.append(f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': finish}]})}")
    if usage:
        lines.append(f"data: {json.dumps({'choices': [], 'usage': usage})}")
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


class FakeAI:
    def __init__(self):
        self.requests = []
        self.status = 200
        self.pieces = ["Here is your email:\n\n```\n", EMAIL, "\n```"]
        self.usage = {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160}

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        if body.get("stream"):
            return httpx.Response(200, content=sse(self.pieces, self.usage),
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "Hi there"}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


@pytest.fixture
def ai(monkeypatch):
    fake = FakeAI()
    monkeypatch.setenv("AI_ASSISTANT_AI_API_KEY", "test-key")
    monkeypatch.setenv("BIGSHIP_API_BASE_URL", "")
    monkeypatch.setattr(assistant_module, "AI_TRANSPORT", httpx.MockTransport(fake))
    return fake


@pytest.fixture
def store(monkeypatch):
    memory = MemoryChatStore()
    monkeypatch.setattr(assistant_module, "CHAT_STORE", memory)
    return memory


@pytest.fixture
def client(ai, store):
    with TestClient(create_app()) as c:
        yield c


def send(client, content, session_id=None, headers=USER, **extra):
    body = {"content": content, **extra}
    if session_id:
        body["session_id"] = session_id
    res = client.post("/api/assistant/chat", json=body, headers=headers)
    events = [json.loads(line) for line in res.text.splitlines() if line.strip()] if res.status_code == 200 else []
    return res, events


def test_new_chat_streams_and_saves_everything(client, ai, store):
    res, events = send(client, "Customer ko delay ke liye sorry email likho")
    assert res.status_code == 200 and res.headers["content-type"].startswith("application/x-ndjson")
    kinds = [e["type"] for e in events]
    assert kinds == ["session", "user_message", "delta", "delta", "delta", "done"]
    assert "".join(e["text"] for e in events if e["type"] == "delta").endswith("\n```")
    done = events[-1]
    assert done["message"]["role"] == "assistant"
    assert done["message"]["parts"] == [{"kind": "text", "text": "Here is your email:"},
                                        {"kind": "copy", "text": EMAIL}]
    assert done["usage"] == {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160}
    day = next(p for p in done["limits"]["periods"] if p["period"] == "day")
    assert day["used"] == 160 and day["limit"] == 50_000 and day["left"] == 49_840
    session = events[0]["session"]
    assert session["title"] == "Customer ko delay ke liye sorry email likho"
    assert ai.requests[-1]["stream"] is True and ai.requests[-1]["stream_options"] == {"include_usage": True}

    # Billing record and saved messages
    assert store.usage[-1] | {"created_at": None} == {
        "user_id": "102795", "session_id": session["id"], "message_id": done["message"]["id"], "endpoint": "chat",
        "model": "deepseek-chat", "prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160,
        "estimated": False, "created_at": None}
    detail = client.get(f"/api/assistant/sessions/{session['id']}", headers=USER).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["content"] == f"Here is your email:\n\n{EMAIL}"


def test_follow_up_sends_the_conversation(client, ai):
    _, first = send(client, "Write a sorry email")
    session_id = first[0]["session"]["id"]
    ai.pieces = ["```\nShorter email\n```"]
    res, events = send(client, "make it shorter", session_id)
    assert res.status_code == 200 and events[0]["session"]["id"] == session_id
    sent = ai.requests[-1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[1]["content"] == "Write a sorry email"
    assert "[customer name]" in sent[2]["content"]          # the raw answer, not the seller's details
    assert sent[3]["content"] == "make it shorter"
    assert "This is a conversation" in sent[0]["content"]
    sessions = client.get("/api/assistant/sessions", headers=USER).json()
    assert len(sessions) == 1 and sessions[0]["id"] == session_id


def test_sessions_are_private_and_can_be_renamed_and_deleted(client, store):
    _, events = send(client, "hello")
    sid = events[0]["session"]["id"]
    assert client.get(f"/api/assistant/sessions/{sid}", headers=OTHER).status_code == 404
    assert client.patch(f"/api/assistant/sessions/{sid}", json={"title": "x"}, headers=OTHER).status_code == 404
    assert client.delete(f"/api/assistant/sessions/{sid}", headers=OTHER).status_code == 404
    res, _ = send(client, "hi", sid, headers=OTHER)
    assert res.status_code == 404 and res.json()["detail"]["code"] == "session_not_found"
    assert client.get("/api/assistant/sessions", headers=OTHER).json() == []

    renamed = client.patch(f"/api/assistant/sessions/{sid}", json={"title": "  Delay   email "}, headers=USER)
    assert renamed.status_code == 200 and renamed.json()["title"] == "Delay email"
    assert client.delete(f"/api/assistant/sessions/{sid}", headers=USER).status_code == 204
    assert client.get(f"/api/assistant/sessions/{sid}", headers=USER).status_code == 404
    assert len(store.usage) == 1                      # the usage record stays for billing


def test_limits_block_new_messages(client, ai, store):
    store.limits["102795"] = {"daily_tokens": 150, "weekly_tokens": None, "monthly_tokens": None}
    res, _ = send(client, "first")                    # 0 used: allowed, uses 160
    assert res.status_code == 200
    res, _ = send(client, "second")
    assert res.status_code == 429
    detail = res.json()["detail"]
    assert detail["code"] == "token_limit_reached" and "today's AI Assistant limit (150 tokens)" in detail["message"]
    assert len(ai.requests) == 1                      # the AI was not called
    usage = client.get("/api/assistant/usage", headers=USER).json()
    assert usage["blocked"] is True and usage["message"] == detail["message"]
    assert {p["period"]: p["limit"] for p in usage["periods"]} == {"day": 150, "week": 250_000, "month": 800_000}
    # Other users are not affected; /ask is limited too.
    assert send(client, "hello", headers=OTHER)[0].status_code == 200
    ask = client.post("/api/assistant/ask", json={"instruction": "hi"}, headers=USER)
    assert ask.status_code == 429


def test_ask_records_usage(client, store):
    res = client.post("/api/assistant/ask", json={"instruction": "hi"}, headers=USER)
    assert res.status_code == 200
    assert store.usage[-1]["endpoint"] == "ask" and store.usage[-1]["total_tokens"] == 15


def test_usage_is_estimated_when_the_ai_reports_none(client, ai, store):
    ai.usage = None
    _, events = send(client, "x" * 30)
    assert store.usage[-1]["estimated"] is True
    assert events[-1]["usage"]["completion_tokens"] > 0


def test_ai_errors_before_the_answer_are_http_errors(client, ai, store):
    ai.status = 429
    res, _ = send(client, "hello")
    assert res.status_code == 429 and res.json()["detail"]["code"] == "ai_rate_limited"
    assert store.sessions == {} and store.usage == []          # nothing saved or charged


def test_chat_needs_login_and_a_message(client):
    assert client.post("/api/assistant/chat", json={"content": "hi"}).status_code == 401
    assert client.post("/api/assistant/chat", json={"content": " "}, headers=USER).status_code == 422
    assert client.post("/api/assistant/chat", json={"content": "hi", "session_id": "nope"},
                       headers=USER).status_code == 422
    assert client.get("/api/assistant/sessions/not-a-uuid", headers=USER).status_code == 404


def test_without_a_database_chat_is_off_but_ask_works(ai, monkeypatch):
    monkeypatch.setattr(assistant_module, "CHAT_STORE", None)
    with TestClient(create_app()) as c:
        res, _ = send(c, "hello")
        assert res.status_code == 503 and res.json()["detail"]["code"] == "chat_history_unavailable"
        assert c.post("/api/assistant/ask", json={"instruction": "hi"}, headers=USER).status_code == 200


def test_history_keeps_the_newest_turns_within_the_budget():
    rows = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + "x" * 100, "files": [],
             "file_context": ""} for i in range(10)]
    picked = history_messages(rows, max_messages=6, max_chars=350)
    assert picked[0]["role"] == "user" and picked[-1]["content"].startswith("m9")
    assert len(picked) == 2                                    # m8, m9 fit; m7 would pass 350 chars
    rows = [{"role": "user", "content": "summarise", "files": [{"name": "a.xlsx", "format": "Excel"}],
             "file_context": "\n\nATTACHED FILES " + "y" * 5000}]
    assert "content no longer included" in history_messages(rows, 20, 2000)[0]["content"]


def test_ist_windows():
    now = datetime(2026, 11, 24, 20, 0, tzinfo=timezone.utc)       # Wed 25 Nov 01:30 IST
    w = windows(now)
    assert w["day"][0] == datetime(2026, 11, 25, tzinfo=IST)
    assert w["week"][0] == datetime(2026, 11, 23, tzinfo=IST)     # Monday
    assert w["month"] == (datetime(2026, 11, 1, tzinfo=IST), datetime(2026, 12, 1, tzinfo=IST))
    assert w["day"][1] - w["day"][0] == timedelta(days=1)
