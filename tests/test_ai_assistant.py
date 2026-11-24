"""POST /api/assistant/ask with a fake OpenAI-compatible AI."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.ai_assistant import module as assistant_module
from app.modules.ai_assistant.engine import AssistantError, split_answer

URL = "/api/assistant/ask"
USER = {"X-User-Id": "102795"}
EMAIL = ("Subject: Sorry for the delay in your order\n\nDear [customer name],\n\nWe are sorry that your order "
         "[order number] is late. It will reach you by [new date].\n\nRegards,\n[your name]")


def reply(content, finish="stop"):
    return lambda: httpx.Response(200, json={"choices": [{"finish_reason": finish, "message": {"content": content}}],
                                             "usage": {"prompt_tokens": 10, "completion_tokens": 20}})


class FakeAI:
    def __init__(self):
        self.requests = []
        self.answer = reply(f"Here is a polite email you can send:\n\n```\n{EMAIL}\n```\n\nAdd the new date before sending.")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer()


LOGIN = {**USER, "X-Shipment-Authorization": "Bearer 12|abcdefghijklmnop"}
PROFILE = {"cust_id": 4242, "first_name": "Ravi", "last_name": "Kumar", "company_name": "Ravi Fashion House",
           "company_mobile_no": "9876500000", "mobile_no": "9000000000", "company_email_id": "null",
           "email_id": "ravi@shop.example", "company_website_url": ""}


class FakeBigShip:
    def __init__(self):
        self.calls = 0
        self.answer = lambda: httpx.Response(200, json={"status": True, "data": {"user_profile_data": PROFILE}})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/get-user-profile"
        assert request.headers["Authorization"] == LOGIN["X-Shipment-Authorization"]
        self.calls += 1
        return self.answer()


@pytest.fixture
def bigship(monkeypatch):
    fake = FakeBigShip()
    monkeypatch.setenv("BIGSHIP_API_BASE_URL", "https://bigship.test")
    monkeypatch.setattr(assistant_module, "BIGSHIP_TRANSPORT", httpx.MockTransport(fake))
    return fake


@pytest.fixture
def ai(monkeypatch, bigship):
    fake = FakeAI()
    monkeypatch.setenv("AI_ASSISTANT_AI_API_KEY", "test-key")
    monkeypatch.setattr(assistant_module, "AI_TRANSPORT", httpx.MockTransport(fake))
    return fake


@pytest.fixture
def client(ai):
    with TestClient(create_app()) as c:
        yield c


def sent(ai):
    return json.loads(ai.requests[-1].content)


def test_answers_the_task_directly(client, ai):
    res = client.post(URL, json={"instruction": "Late delivery ke liye customer ko sorry email likho"}, headers=USER)
    assert res.status_code == 200
    assert res.json()["parts"] == [
        {"kind": "text", "text": "Here is a polite email you can send:"},
        {"kind": "copy", "text": EMAIL},
        {"kind": "text", "text": "Add the new date before sending."},
    ]
    assert res.json()["answer"] == f"Here is a polite email you can send:\n\n{EMAIL}\n\nAdd the new date before sending."
    request = ai.requests[-1]
    assert request.url == "https://api.deepseek.com/chat/completions"
    assert request.headers["Authorization"] == "Bearer test-key"
    body = sent(ai)
    assert body["model"] == "deepseek-chat" and body["max_tokens"] == 2000 and "response_format" not in body
    system, user = body["messages"]
    assert "Do the task directly" in system["content"] and "starts with a line ```" in system["content"]
    assert "pharmacist" in system["content"] and "Never give a personal dose" in system["content"]
    assert user == {"role": "user", "content": "Late delivery ke liye customer ko sorry email likho"}


SIGNED = ("Report for [Shop Name]\n```\nHello [customer name],\nCall us on [your phone] or write to "
          "[your email].\nVisit [your website].\nRegards,\n[your name]\n[shop name]\n```")


def test_the_sellers_own_details_are_filled_in_after_the_ai(client, ai, bigship):
    ai.answer = reply(SIGNED)
    res = client.post(URL, json={"instruction": "Write a delivery report for my shop [shop name]"}, headers=LOGIN)
    assert res.status_code == 200
    assert res.json()["parts"] == [
        {"kind": "text", "text": "Report for Ravi Fashion House"},
        {"kind": "copy", "text": "Hello [customer name],\nCall us on 9876500000 or write to ravi@shop.example.\n"
                                 "Visit [your website].\nRegards,\nRavi Kumar\nRavi Fashion House"},
    ]
    sent_text = json.dumps(sent(ai))
    assert "Ravi" not in sent_text and "9876500000" not in sent_text   # the AI never sees them
    assert "[shop name], [your name], [your phone]" in sent(ai)["messages"][0]["content"]
    client.post(URL, json={"instruction": "again"}, headers=LOGIN)
    assert bigship.calls == 1   # cached per login


@pytest.mark.parametrize("profile_answer", [
    lambda: httpx.Response(401, json={"message": "Unauthenticated."}),
    lambda: httpx.Response(500, json={}),
    lambda: httpx.Response(200, json={"data": {}}),
])
def test_answer_keeps_placeholders_when_the_profile_is_unavailable(client, ai, bigship, profile_answer):
    ai.answer = reply(SIGNED)
    bigship.answer = profile_answer
    res = client.post(URL, json={"instruction": "report"}, headers=LOGIN)
    assert res.status_code == 200
    assert "[your name]\n[shop name]" in res.json()["answer"]


def test_without_a_login_bigship_is_not_called(client, ai, bigship):
    ai.answer = reply(SIGNED)
    assert "[shop name]" in client.post(URL, json={"instruction": "report"}, headers=USER).json()["answer"]
    assert bigship.calls == 0


def test_reply_language_and_task_chip_are_passed_to_the_ai(client, ai):
    res = client.post(URL, json={"instruction": "Paracetamol kis kaam aati hai", "language": "hindi",
                                 "task": "medicine"}, headers=USER)
    assert res.status_code == 200
    system, user = sent(ai)["messages"]
    assert "in Hindi, in Devanagari script" in system["content"]
    assert "The user picked: Medicine info." in system["content"]
    assert user["content"] == "Paracetamol kis kaam aati hai"


def test_same_language_and_anything_add_no_rule(client, ai):
    client.post(URL, json={"instruction": "hello", "task": "anything"}, headers=USER)
    assert "For this request" not in sent(ai)["messages"][0]["content"]
    client.post(URL, json={"instruction": "hello", "language": "hinglish"}, headers=USER)
    assert "Hinglish (Hindi written in English letters)" in sent(ai)["messages"][0]["content"]
    assert client.post(URL, json={"instruction": "hi", "task": "poem"}, headers=USER).status_code == 422


def test_personal_data_is_masked_before_the_ai(client, ai):
    res = client.post(URL, json={"instruction": "sorry email for ravi@example.com, phone 9876543210"}, headers=USER)
    assert res.status_code == 200
    text = sent(ai)["messages"][1]["content"]
    assert "ravi@example.com" not in text and "9876543210" not in text


@pytest.mark.parametrize("body", [
    {"instruction": ""},
    {"instruction": "   "},
    {"instruction": "x" * 2001},
    {"instruction": "caption", "language": "french"},
])
def test_invalid_requests_never_reach_the_ai(client, ai, body):
    assert client.post(URL, json=body, headers=USER).status_code == 422
    assert ai.requests == []


def test_requires_a_user(client, ai):
    assert client.post(URL, json={"instruction": "Diwali sale ka WhatsApp message"}).status_code == 401
    assert ai.requests == []


@pytest.mark.parametrize("answer, status, code", [
    (reply("   "), 502, "ai_empty_answer"),
    (reply(None), 502, "ai_empty_answer"),
    (reply("Subject: half", finish="length"), 502, "ai_response_truncated"),
    (lambda: httpx.Response(200, json={"choices": []}), 502, "ai_invalid_response"),
    (lambda: httpx.Response(429, json={}), 429, "ai_rate_limited"),
    (lambda: httpx.Response(401, json={"error": "bad key"}), 502, "ai_service_error"),
])
def test_ai_failures_are_errors_never_the_users_text(client, ai, answer, status, code):
    ai.answer = answer
    res = client.post(URL, json={"instruction": "Excel summary banao"}, headers=USER)
    assert res.status_code == status
    assert res.json()["detail"]["code"] == code
    assert "Excel summary" not in res.text


def test_ai_unreachable(client, ai):
    def refuse():
        raise httpx.ConnectError("down")
    ai.answer = refuse
    assert client.post(URL, json={"instruction": "Excel summary"}, headers=USER).status_code == 503


def test_not_configured_without_a_key(monkeypatch):
    monkeypatch.setattr(assistant_module, "AI_TRANSPORT", httpx.MockTransport(lambda r: pytest.fail("AI called")))
    with TestClient(create_app()) as c:
        res = c.post(URL, json={"instruction": "Excel summary"}, headers=USER)
    assert res.status_code == 503
    assert res.json()["detail"]["code"] == "ai_assistant_not_configured"


def test_an_explanation_without_a_block_is_one_plain_text_part():
    raw = "## Paracetamol\n**Use:** fever and pain\n* Take after food\n\n\n\nAsk a doctor."
    assert split_answer(raw) == [{"kind": "text",
                                  "text": "Paracetamol\nUse: fever and pain\n- Take after food\n\nAsk a doctor."}]
    assert len(split_answer("word " * 3000)[0]["text"]) == 7999
    for empty in (None, "   ", "```\n\n```"):
        with pytest.raises(AssistantError):
            split_answer(empty)


def test_blocks_become_copy_parts_in_order():
    raw = ('Yeh 2 messages hain:\n```text\n"Diwali sale: 20% off!"\n```\n'
           'Doosra:\n  ```\n**Happy Diwali** from [shop name]\n  ```')
    assert split_answer(raw) == [
        {"kind": "text", "text": "Yeh 2 messages hain:"},
        {"kind": "copy", "text": "Diwali sale: 20% off!"},
        {"kind": "text", "text": "Doosra:"},
        {"kind": "copy", "text": "Happy Diwali from [shop name]"},
    ]


def test_an_unclosed_block_runs_to_the_end():
    assert split_answer("Message:\n```\nSale is live!") == [
        {"kind": "text", "text": "Message:"}, {"kind": "copy", "text": "Sale is live!"}]


# ── Attached files (Excel, Word, PDF) ──────────────────────────────────────────

def xlsx(rows):
    import io
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = "Orders"
    for row in rows:
        sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def docx(paragraphs):
    import io
    from docx import Document
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def upload(client, name, blob, user=USER):
    res = client.post("/api/attachments", files={"file": (name, blob, "application/octet-stream")}, headers=user)
    assert res.status_code == 201, res.text
    return res.json()["attachment_id"]


def test_excel_and_word_files_are_read_with_the_prompt(client, ai):
    sheet = upload(client, "orders.xlsx", xlsx([["Order", "City", "Amount"], ["A-1", "Pune", 500], ["A-2", "Delhi", 250]]))
    letter = upload(client, "policy.docx", docx(["Returns are accepted within 7 days.", "Refunds take 5 days."]))
    res = client.post(URL, json={"instruction": "Total amount kitna hai?", "attachment_ids": [sheet, letter]},
                      headers=USER)
    assert res.status_code == 200, res.text
    system, user = sent(ai)["messages"]
    assert "ATTACHED FILES" in system["content"]
    text = user["content"]
    assert text.startswith("Total amount kitna hai?")
    assert "=== File: orders.xlsx (Excel) ===" in text and "[Orders] 3 rows x 3 columns" in text
    # Rows arrive tab-separated; the privacy filter may mask a cell it takes for a name ("A-1 Pune").
    assert "\t500\n" in text and "A-2\tDelhi\t250" in text
    assert "=== File: policy.docx (Word) ===" in text and "Returns are accepted within 7 days." in text


def test_files_alone_ask_for_a_summary(client, ai):
    letter = upload(client, "note.docx", docx(["Pickup is at 5 pm daily."]))
    assert client.post(URL, json={"attachment_ids": [letter]}, headers=USER).status_code == 200
    assert sent(ai)["messages"][1]["content"].startswith("Summarise the attached file(s).")


def test_another_users_or_a_removed_file_is_refused(client, ai):
    other = upload(client, "theirs.docx", docx(["secret plan"]), user={"X-User-Id": "999"})
    res = client.post(URL, json={"instruction": "read it", "attachment_ids": [other]}, headers=USER)
    assert res.status_code == 410 and res.json()["detail"]["code"] == "attachment_expired"
    missing = client.post(URL, json={"instruction": "read it", "attachment_ids": ["f" * 32]}, headers=USER)
    assert missing.status_code == 410
    assert ai.requests == []


@pytest.mark.parametrize("ids", [["not-an-id"], ["a" * 32] * 2, [f"{i:032x}" for i in range(6)]])
def test_invalid_file_lists_are_rejected(client, ai, ids):
    assert client.post(URL, json={"instruction": "x", "attachment_ids": ids}, headers=USER).status_code == 422


def test_a_large_sheet_is_cut_and_marked():
    from app.modules.ai_assistant.files import files_block
    rows = [["Order", "Note"]] + [[f"A-{i}", "x" * 150] for i in range(2000)]
    meta = {"filename": "big.xlsx", "inspection": {"format": "xlsx", "units": [
        {"index": 0, "sheet": "Orders", "rows": rows, "row_count": len(rows), "column_count": 2}]}}
    block = files_block([meta])
    assert len(block) < 61_000
    assert "[truncated: showing the first" in block and "of 2001 rows]" in block
    assert "A-0\t" in block and "A-1999\t" not in block
