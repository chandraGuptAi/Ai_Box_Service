"""POST /api/channel-insights/ask with fake Bigship channel orders and a fake AI."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.ai_assistant import module as assistant_module
from app.modules.channel_insights import module as insights_module
from app.modules.channel_insights.analysis import rupees

URL = "/api/channel-insights/ask"
LOGIN = {"Authorization": "Bearer 12|abcdefghijklmnop"}
NOW = datetime.now(timezone.utc)


def order(number, mode="Prepaid", payment="Paid", amount=500, phone="9876500000", address="12 MG Road",
          pincode="411001", status="Unshipped", when=None):
    return {"ChannelOrderId": number, "ExternalOrderNumber": f"#{number}", "PaymentMode": mode,
            "PaymentStatus": payment, "OrderStatus": status, "TotalAmount": str(amount),
            "CollectableAmount": str(amount if mode == "COD" else 0), "CustomerName": "Asha Verma",
            "CustomerPhone": phone, "ShippingAddress1": address, "Pincode": pincode,
            "OrderDate": (when or NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),
            "LineItemsJson": [{"quantity": 2}], "PackageWeight": "0.5", "PackageLength": "10",
            "PackageWidth": "8", "PackageHeight": None, "channel": {"ChannelName": "nr-store.myshopify.com"}}


ORDERS = [
    order(1001), order(1002, when=NOW), order(1003),                         # ready
    order(1004, mode="COD", payment="Pending", amount=800),                    # COD, normal
    order(1005, mode="COD", payment="Pending", amount=4500),                   # COD, high value
    order(1006, mode="COD", payment="Pending", phone=""),                      # COD, no phone
    order(1007, payment="Pending"),                                            # prepaid, unpaid
    order(1008, payment="Voided"),                                             # never ships
]


class FakeBigship:
    def __init__(self, orders=ORDERS):
        self.orders, self.bodies = orders, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/channel/orders/list"
        assert request.headers["Authorization"] == LOGIN["Authorization"]
        body = json.loads(request.content)
        self.bodies.append(body)
        rows = self.orders[: body["perPage"]]
        return httpx.Response(200, json={"success": True, "data": {"getOrderList": rows,
                                                                    "totalRecords": len(self.orders)}})


class FakeAI:
    def __init__(self):
        self.requests, self.content = [], "Ship 3 prepaid orders now and call the 3 COD buyers first."

    def __call__(self, request):
        self.requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                      "message": {"content": self.content}}]})


@pytest.fixture
def bigship(monkeypatch):
    fake = FakeBigship()
    monkeypatch.setenv("BIGSHIP_API_BASE_URL", "https://bigship.test")
    monkeypatch.setattr(insights_module, "BIGSHIP_TRANSPORT", httpx.MockTransport(fake))
    return fake


@pytest.fixture
def ai(monkeypatch):
    fake = FakeAI()
    monkeypatch.setenv("AI_ASSISTANT_AI_API_KEY", "test-key")
    monkeypatch.setattr(assistant_module, "AI_TRANSPORT", httpx.MockTransport(fake))
    return fake


def ask(question, **extra):
    with TestClient(create_app()) as c:
        return c.post(URL, json={"question": question, **extra}, headers=LOGIN)


def metrics(res):
    return {m["label"]: m["value"] for m in res.json()["metrics"]}


def test_what_to_ship_counts_buckets_and_reads_every_store(bigship, ai):
    res = ask("What should I ship today?")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["intent"] == "ship" and data["title"] == "Ready to ship"
    assert data["answer"] == ai.content and data["ai_written"] is True
    assert metrics(res) == {"Ready": "3", "Need a call": "3", "High risk": "2", "Need fixes": "1"}
    assert data["highlight"] == ["#1001", "#1003", "#1002"]        # oldest first
    assert [(a["kind"], a["group"], a["count"]) for a in data["actions"]] == [
        ("book", "ready", 3), ("show", "ready", 3), ("call", "cod", 3), ("show", "needs_fixing", 1)]
    assert data["open_orders"] == 7 and data["partial"] is False   # the voided order is left out
    assert bigship.bodies == [{"page": 1, "perPage": 1000, "status": "Unshipped"}]
    assert data["follow_ups"] == ["Which orders are high risk?"]   # the chips, minus the one just asked


def test_ai_never_sees_customer_details(bigship, ai):
    ask("Is there anything strange in my orders?")
    sent = json.dumps(ai.requests[-1], ensure_ascii=False)
    for secret in ("Asha", "9876500000", "MG Road", "411001"):
        assert secret not in sent
    assert "#1005" in sent                                          # free questions get the order list


def test_high_risk_orders_with_reasons(bigship, ai):
    data = ask("Which orders are high risk?").json()
    assert data["intent"] == "risk"
    assert data["highlight"] == ["#1005", "#1006"]                  # biggest amount first
    risky = {o["order_number"]: o["reasons"] for o in data["orders"] if o["high_risk"]}
    assert risky == {"#1005": ["high COD value (₹4,500)"], "#1006": ["no phone number"]}
    first = data["orders"][0]
    assert first["pincode"] == "411001" and first["total_amount"] == 500
    assert first["package"] == {"weight_kg": 0.5, "length_cm": 10, "width_cm": 8, "height_cm": None}
    assert data["actions"][-1] == {"kind": "call", "label": "Call 2 high-risk orders", "group": "high_risk",
                                   "count": 2}


def test_cod_saving_estimate_without_ai(bigship):
    data = ask("COD confirm karke kitna bachega?").json()
    assert data["intent"] == "cod" and data["ai_written"] is False
    # 3 COD orders x 25% come back x 50% avoided x ₹150 = ₹56
    assert {m["label"]: m["value"] for m in data["metrics"]} == {
        "COD orders": "3", "COD value": "₹5,800", "Est. saving": "₹56"}
    assert data["answer"].startswith("Confirming your 3 COD orders (₹5,800) could save about ₹56")


def test_ai_failure_falls_back_to_the_built_in_answer(bigship, ai):
    ai.content = ""
    data = ask("Summarise today's orders").json()
    assert data["intent"] == "summary" and data["ai_written"] is False
    assert data["answer"].startswith("You have 7 open orders worth")
    assert data["highlight"] == ["#1002"]                           # placed today


def test_store_filter_and_large_stores(monkeypatch, ai):
    fake = FakeBigship([order(n) for n in range(1, 1201)])
    monkeypatch.setenv("BIGSHIP_API_BASE_URL", "https://bigship.test")
    monkeypatch.setattr(insights_module, "BIGSHIP_TRANSPORT", httpx.MockTransport(fake))
    data = ask("What should I ship today?", channel_id=7).json()
    assert [b["perPage"] for b in fake.bodies] == [1000, 1200] and fake.bodies[0]["channel_id"] == 7
    assert data["open_orders"] == 1200 and len(data["orders"]) == 300


def test_no_open_orders(monkeypatch):
    monkeypatch.setenv("BIGSHIP_API_BASE_URL", "https://bigship.test")
    monkeypatch.setattr(insights_module, "BIGSHIP_TRANSPORT", httpx.MockTransport(FakeBigship([])))
    data = ask("What should I ship today?").json()
    assert data["answer"].startswith("You have no open orders") and data["actions"] == []


@pytest.mark.parametrize("body", [{"question": "   "}, {"question": "x" * 301}, {}])
def test_bad_questions(bigship, body):
    with TestClient(create_app()) as c:
        assert c.post(URL, json=body, headers=LOGIN).status_code == 422


def test_login_required(bigship):
    with TestClient(create_app()) as c:
        assert c.post(URL, json={"question": "hi"}).status_code == 401


def test_bigship_rejects_the_token(monkeypatch):
    monkeypatch.setenv("BIGSHIP_API_BASE_URL", "https://bigship.test")
    monkeypatch.setattr(insights_module, "BIGSHIP_TRANSPORT",
                        httpx.MockTransport(lambda r: httpx.Response(401, json={"message": "expired"})))
    assert ask("What should I ship today?").status_code == 401


def test_rupees_uses_indian_grouping():
    assert [rupees(v) for v in (0, 999, 1000, 167000, 12345678.4)] == [
        "₹0", "₹999", "₹1,000", "₹1,67,000", "₹1,23,45,678"]



class FakeSuggestions:
    def __init__(self, chips):
        self.chips = chips

    async def list(self):
        if isinstance(self.chips, Exception):
            raise self.chips
        return self.chips


def test_suggestions_come_from_the_table(bigship, monkeypatch):
    monkeypatch.setattr(insights_module, "SUGGESTION_STORE", FakeSuggestions(["Which orders are high risk?", "Hi"]))
    with TestClient(create_app()) as c:
        assert c.get("/api/channel-insights/suggestions").json() == ["Which orders are high risk?", "Hi"]


def test_suggestions_fall_back_without_a_database(bigship, monkeypatch):
    with TestClient(create_app()) as c:                              # no DATABASE_URL in tests
        assert c.get("/api/channel-insights/suggestions").json() == ["Which orders are high risk?"]
    monkeypatch.setattr(insights_module, "SUGGESTION_STORE", FakeSuggestions(RuntimeError("db down")))
    with TestClient(create_app()) as c:
        assert c.get("/api/channel-insights/suggestions").json() == ["Which orders are high risk?"]
