"""The privacy filter masks personal data and never rejects a question."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app()) as c:
        yield c


def run(client, text, identities=()):
    res = client.post("/api/privacy/filter", json={"messages": [{"role": "user", "content": text}],
                                                   "identities": list(identities)})
    assert res.status_code == 200
    body = res.json()
    return body["messages"][0]["content"], sorted(r["category"] for r in body["removed"])


@pytest.mark.parametrize("text", [
    "चयनित तिथि सीमा में सभी ऑर्डर का स्टेटस ब्रेकडाउन दें, जिसमें प्रत्येक स्टेटस के लिए गिनती और प्रतिशत दिखाए जाएं।",
    "pichle 7 din ke sabhi orders ka status breakdown do, har status ka count aur percentage dikhao",
    "Delhi wale RTO shipments ki list banao aur courier wise compare karo",
    "दिल्ली में कितने शिपमेंट वापस आए",
    "Shree Maruti courier ka RTO rate batao",
])
def test_report_questions_pass_unchanged(client, text):
    assert run(client, text) == (text, [])


@pytest.mark.parametrize("text, secret", [
    ("Ramesh Kumar ke orders dikhao", "Ramesh"),
    ("मेरा नाम रमेश है, मेरे ऑर्डर दिखाओ", "रमेश"),
    ("my name is Priya Nair; call 9876543210", "9876543210"),
    ("Rahul Sharma requested a shipment report.", "Rahul"),
])
def test_personal_data_is_masked_not_blocked(client, text, secret):
    clean, removed = run(client, text)
    assert secret not in clean and "[REDACTED]" in clean and removed


def test_known_identity_and_structured_fields(client):
    clean, removed = run(client, '{"customer_name": "Asha", "orders": 4} for Indu Enterprises', ["Indu Enterprises"])
    assert "Asha" not in clean and "Indu Enterprises" not in clean and '"orders": 4' in clean
    assert removed == ["field_name", "known_identity"]


def test_invalid_role_is_a_validation_error(client):
    res = client.post("/api/privacy/filter", json={"messages": [{"role": "tool", "content": "x"}]})
    assert res.status_code == 422
