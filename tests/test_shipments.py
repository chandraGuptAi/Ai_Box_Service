"""Shipments module: complete fetch, personal data removed at the source, cache, error codes."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.shipments.fetch import Limits, ShipmentFetcher

TOKEN = {"Authorization": "Bearer seller-token"}
QUERY = {"status_type": 0, "from_date": "2026-10-22 00:00:00", "to_date": "2026-10-30 00:00:00"}


def record(i):
    # Shape of a real /api/get-order-list row (see the Bigship order list response).
    return {"orderId": str(240213000 + i), "awb_number": str(19041973400000 + i), "lane": "N1-E2" if i % 2 else "A-A",
            "courierName": "Delhivery 0.5 Special", "status": "Cancelled", "payment_mode": "Prepaid",
            "orderDate": "2026-10-29 08:10:00.0000000", "totalCharge": 70.5, "invoiceTotalAmount": "1500.00",
            "products_name": "Belts, Shoes", "invoiceFileURL": "https://storage.bigship.direct/files/x.pdf",
            "EwaybillNumber": None,
            "consignor": {"companyName": "Vaibhav Gupta", "companyEmailId": "indu.rautela@bigship.in",
                          "companyMobile": "8114424395", "billingAddress": "house -812, vihar colony",
                          "country": "India", "state": "RAJASTHAN", "city": "KOTA", "pin": "325219",
                          "consignorBusinessType": "Individual"},
            "consignee": {"companyName": "Gupta Enterprise", "companyEmailId": "vaibhav.gupta@bigship.in",
                          "companyMobile": "9898787676", "billingAddress": "Road-298,Testing Address",
                          "country": "India", "state": "DELHI", "city": "DELHI", "pin": "110011"},
            "domestic_transit_stats": {"courier_remarks": None, "date_delivery": None}}


@pytest.fixture
def service():
    rows = [record(i) for i in range(250)]
    calls = []

    def handler(request):
        if request.headers.get("Authorization") != "Bearer seller-token":
            return httpx.Response(401)
        body = json.loads(request.content)
        calls.append(body)
        start = (body["page"] - 1) * body["perPage"]
        return httpx.Response(200, json={"success": True, "status_code": 200, "data": {
            "getOrderList": rows[start:start + body["perPage"]], "totalRecords": len(rows), "all_orders": 900}})

    with TestClient(create_app()) as client:
        client.app.state.shipments_fetcher = ShipmentFetcher(
            "https://bigship.test", Limits(min_page_size=50), transport=httpx.MockTransport(handler))
        yield client, calls


def test_every_shipment_is_returned_without_personal_data(service):
    client, calls = service
    res = client.post("/api/shipments/fetch", json=QUERY, headers=TOKEN)
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == len(body["rows"]) == 250 and body["cached"] is False
    assert len({r["orderId"] for r in body["rows"]}) == 250
    sent = json.dumps(body["rows"])
    for secret in ("Vaibhav Gupta", "Gupta Enterprise", "bigship.in", "8114424395", "9898787676",
                   "vihar colony", "Testing Address", "storage.bigship.direct"):
        assert secret not in sent
    row = body["rows"][1]
    assert row["consignee"] == {"country": "India", "state": "DELHI", "city": "DELHI", "pin": "110011"}
    assert row["consignor"]["pin"] == "325219" and row["products_name"] == "Belts, Shoes"
    assert (row["lane"], row["origin_zone"], row["destination_zone"]) == ("N1-E2", "N1", "E2")
    removed = {(f["field"], f["reason"]): f["count"] for f in body["removed_fields"]}
    assert removed[("consignee.companyName", "person or business name")] == 250
    assert removed[("consignor.companyMobile", "contact detail")] == 250
    assert removed[("invoiceFileURL", "file link")] == 250
    assert not any(f["field"] == "EwaybillNumber" for f in body["removed_fields"])  # empty values are not counted
    assert body["account_summary"] == {"all_orders": 900}


def test_repeat_request_is_served_from_cache_unless_fresh(service):
    client, calls = service
    client.post("/api/shipments/fetch", json=QUERY, headers=TOKEN)
    upstream = len(calls)
    again = client.post("/api/shipments/fetch", json=QUERY, headers=TOKEN).json()
    assert again["cached"] is True and len(calls) == upstream
    fresh = client.post("/api/shipments/fetch", json={**QUERY, "fresh": True}, headers=TOKEN).json()
    assert fresh["cached"] is False and len(calls) > upstream


def test_other_seller_does_not_read_the_cache(service):
    client, calls = service
    client.post("/api/shipments/fetch", json=QUERY, headers=TOKEN)
    res = client.post("/api/shipments/fetch", json=QUERY, headers={"Authorization": "Bearer other-seller"})
    assert res.status_code == 401
    assert res.json()["detail"]["code"] == "upstream_auth_failed"


def test_record_limit_is_reported_with_its_code(service):
    client, _ = service
    res = client.post("/api/shipments/fetch", json={**QUERY, "max_records": 100}, headers=TOKEN)
    assert res.status_code == 422 and res.json()["detail"]["code"] == "source_too_large"


def test_bigship_token_is_required(service):
    client, _ = service
    assert client.post("/api/shipments/fetch", json=QUERY).status_code == 401
