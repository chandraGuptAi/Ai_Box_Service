import json
import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.channel_wise_report.client import BigshipChannelClient
from app.modules.channel_wise_report.service import Service

TOKEN = "Bearer test-token"

# Shape copied from what the Angular panel reads off GET /api/channel.
BIGSHIP_CHANNELS = [
    {"ChannelId": 11, "ChannelName": "OMSGuru Store", "ShopDomain": None, "PlatformType": "OMSGuru",
     "Status": "Connected", "IsActive": "1", "shopify_freemium": None,
     "LastOrderSync": "2026-10-30 05:50:00", "LastInventorySync": "2026-10-30 05:50:00",
     "CreatedAt": "2026-10-01 10:00:00"},
    {"ChannelId": 12, "ChannelName": "https://www.bigshipstore.com/", "ShopDomain": "bigshipstore.com",
     "PlatformType": "WooCommerce", "Status": "Connected", "IsActive": "1", "shopify_freemium": 0,
     "LastOrderSync": "2026-08-27 12:29:00", "LastInventorySync": None, "CreatedAt": None},
    {"ChannelId": 13, "ChannelName": "nr-store-aa8kdjyj.myshopify.com", "ShopDomain": "nr-store-aa8kdjyj.myshopify.com",
     "PlatformType": "Shopify", "Status": "Failed", "IsActive": "0", "shopify_freemium": 0,
     "LastOrderSync": "2026-09-06T08:00:00Z", "LastInventorySync": "garbage", "CreatedAt": None},
]


@pytest.fixture
def make_client():
    """Start the app, then point the module at a fake Bigship API served by `handler`."""
    clients = []

    def factory(handler):
        app = create_app()
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        bigship = BigshipChannelClient("https://bigship.test", 5, transport=httpx.MockTransport(handler))
        app.state.channel_wise_report_service = Service(bigship)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def ok_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/api/channel"
    assert request.headers["authorization"] == TOKEN
    return httpx.Response(200, json={"status": True, "data": BIGSHIP_CHANNELS})


def test_lists_and_normalizes_channels(make_client):
    client = make_client(ok_handler)
    res = client.get("/api/channel_wise_report/channels", headers={"Authorization": TOKEN})
    assert res.status_code == 200
    body = res.json()

    assert body["summary"] == {"total": 3, "connected": 2, "not_connected": 1,
                               "by_platform": {"OMSGuru": 1, "WooCommerce": 1, "Shopify": 1}}
    oms, woo, shopify = body["channels"]
    assert oms["display_name"] == "OMSGuru Store"
    assert oms["last_order_sync"] == "2026-10-30T05:50:00Z"
    assert woo["display_name"] == "Bigshipstore.com"
    assert woo["plan_type"] == "0"
    assert shopify["display_name"] == "Nr-store-aa8kdjyj"
    assert shopify["is_connected"] is False and shopify["is_active"] is False
    assert shopify["last_inventory_sync"] is None


def test_requires_bearer_token(make_client):
    client = make_client(ok_handler)
    assert client.get("/api/channel_wise_report/channels").status_code == 401


@pytest.mark.parametrize("response, expected", [
    (httpx.Response(401, json={"message": "Unauthenticated"}), 401),
    (httpx.Response(500, text="boom"), 502),
    (httpx.Response(200, json={"status": False, "message": "No channels"}), 502),
    (httpx.Response(200, text="not json"), 502),
])
def test_upstream_errors(make_client, response, expected):
    client = make_client(lambda request: response)
    res = client.get("/api/channel_wise_report/channels", headers={"Authorization": TOKEN})
    assert res.status_code == expected


def test_upstream_timeout(make_client):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = make_client(handler)
    res = client.get("/api/channel_wise_report/channels", headers={"Authorization": TOKEN})
    assert res.status_code == 504


def test_accepts_x_shipment_authorization_from_gateway(make_client):
    client = make_client(ok_handler)
    res = client.get("/api/channel_wise_report/channels", headers={"X-Shipment-Authorization": TOKEN})
    assert res.status_code == 200


def test_service_token_required_when_configured(make_client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("SERVICE_TOKEN", "gateway-secret")
    get_settings.cache_clear()
    try:
        client = make_client(ok_handler)
        url = "/api/channel_wise_report/channels"
        assert client.get(url, headers={"Authorization": TOKEN}).status_code == 403
        assert client.get(url, headers={"Authorization": TOKEN, "X-Ai-Box-Service-Token": "wrong"}).status_code == 403
        ok = client.get(url, headers={"Authorization": TOKEN, "X-Ai-Box-Service-Token": "gateway-secret"})
        assert ok.status_code == 200
        assert client.get("/api/health").status_code == 200
    finally:
        monkeypatch.setenv("SERVICE_TOKEN", "")
        get_settings.cache_clear()


def raw_order(n: int, **extra) -> dict:
    return {"ChannelOrderId": n, "ExternalOrderNumber": f"#{1000 + n}", "OrderDate": "2026-10-29 10:00:00",
            "OrderStatus": "Unshipped", "PaymentStatus": "Paid", "FulfillmentStatus": "unfulfilled",
            "PaymentMode": "Prepaid", "TotalAmount": "100.50", "CollectableAmount": 0,
            "LineItemsJson": json.dumps([{"quantity": 2, "fulfillable_quantity": 2}]),
            "CustomerName": "Secret Person", "CustomerPhone": "9999999999", "ShippingAddress1": "House 1",
            "City": "Delhi", "State": "Delhi", "Pincode": "110001", "Country": "India", **extra}


def orders_handler(total: int, seen_bodies: list):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/channel/orders/list"
        body = json.loads(request.content)
        seen_bodies.append(body)
        start = (body["page"] - 1) * body["perPage"]
        rows = [raw_order(n) for n in range(start + 1, min(start + body["perPage"], total) + 1)]
        return httpx.Response(200, json={"status": True, "data": {"getOrderList": rows, "totalRecords": total}})
    return handler


def test_channel_orders_reads_every_page(make_client):
    bodies: list = []
    client = make_client(orders_handler(2500, bodies))
    res = client.get("/api/channel_wise_report/channels/13/orders?status=Unshipped", headers={"Authorization": TOKEN})
    assert res.status_code == 200
    body = res.json()

    # Call 1 learns totalRecords from a 1000-row page; call 2 asks for all 2500 at once.
    assert [(b["page"], b["perPage"]) for b in bodies] == [(1, 1000), (1, 2500)]
    assert all(b["status"] == "Unshipped" and b["channel_id"] == 13 for b in bodies)
    assert body["total_records"] == 2500 and len(body["orders"]) == 2500
    assert body["summary"]["total_orders"] == 2500
    assert body["summary"]["total_amount"] == 251250.0
    assert body["summary"]["by_fulfillment_status"] == {"Unfulfilled": 2500}

    order = body["orders"][0]
    assert order["order_number"] == "#1001" and order["items_count"] == 2 and order["city"] == "Delhi"
    assert "Secret Person" not in res.text and "9999999999" not in res.text and "House 1" not in res.text


def test_channel_orders_empty_channel(make_client):
    bodies: list = []
    client = make_client(orders_handler(0, bodies))
    res = client.get("/api/channel_wise_report/channels/13/orders", headers={"Authorization": TOKEN})
    assert res.status_code == 200
    assert res.json()["orders"] == [] and len(bodies) == 1 and bodies[0]["status"] == "all"


def test_channel_orders_rejects_bad_input(make_client):
    client = make_client(orders_handler(0, []))
    assert client.get("/api/channel_wise_report/channels/13/orders?status=Bogus",
                      headers={"Authorization": TOKEN}).status_code == 422
    assert client.get("/api/channel_wise_report/channels/0/orders", headers={"Authorization": TOKEN}).status_code == 422


def test_channel_orders_over_limit(make_client):
    client = make_client(orders_handler(999_999, []))
    res = client.get("/api/channel_wise_report/channels/13/orders", headers={"Authorization": TOKEN})
    assert res.status_code == 413


def test_channel_orders_small_channel_needs_one_call(make_client):
    bodies: list = []
    client = make_client(orders_handler(999, bodies))
    res = client.get("/api/channel_wise_report/channels/13/orders", headers={"Authorization": TOKEN})
    assert res.status_code == 200
    assert len(res.json()["orders"]) == 999 and [b["perPage"] for b in bodies] == [1000]


def test_channel_orders_date_filter_uses_ist_day(make_client):
    rows = [raw_order(1, OrderDate="2026-10-27 18:29:00"),   # 27 Oct 23:59 IST -> outside
            raw_order(2, OrderDate="2026-10-27 18:30:00"),   # 28 Oct 00:00 IST -> inside
            raw_order(3, OrderDate="2026-10-29 18:29:59"),   # 29 Oct 23:59 IST -> inside
            raw_order(4, OrderDate=None)]                    # no date -> outside

    def handler(request):
        return httpx.Response(200, json={"status": True, "data": {"getOrderList": rows, "totalRecords": 4}})

    client = make_client(handler)
    res = client.get("/api/channel_wise_report/channels/13/orders?from_date=2026-10-28&to_date=2026-10-29",
                     headers={"Authorization": TOKEN})
    body = res.json()
    assert [o["channel_order_id"] for o in body["orders"]] == [2, 3]
    assert body["total_records"] == 4 and body["summary"]["total_orders"] == 2
    assert client.get("/api/channel_wise_report/channels/13/orders?from_date=2026-10-29&to_date=2026-10-28",
                      headers={"Authorization": TOKEN}).status_code == 422


def test_preview_count_shipments_sends_page_body(make_client):
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"success": True, "data": {"getOrderList": [{}], "totalRecords": 46}})

    client = make_client(handler)
    res = client.get("/api/channel_wise_report/preview-count?from_date=2026-10-24&to_date=2026-10-30&status_type=3",
                     headers={"Authorization": TOKEN})
    assert res.json() == {"count": 46, "source": "shipments"}
    path, body = seen[0]
    assert path == "/api/get-order-list"
    assert (body["type"], body["page"], body["perPage"]) == (3, 1, 1)
    # IST midnight boundaries as UTC, end exclusive — same as ShipmentDataService.payload().
    assert (body["fromDate"], body["toDate"]) == ("2026-10-23 18:30:00", "2026-10-30 18:30:00")


def test_preview_count_channel_orders(make_client):
    rows = [raw_order(1, OrderDate="2026-10-28 06:00:00"), raw_order(2, OrderDate="2026-09-01 06:00:00")]

    def handler(request):
        assert request.url.path == "/api/channel/orders/list"
        return httpx.Response(200, json={"status": True, "data": {"getOrderList": rows, "totalRecords": 2}})

    client = make_client(handler)
    res = client.get("/api/channel_wise_report/preview-count?from_date=2026-10-24&to_date=2026-10-30&channel_id=13",
                     headers={"Authorization": TOKEN})
    assert res.json() == {"count": 1, "source": "channel_orders"}


def test_preview_count_upstream_failure_flag(make_client):
    client = make_client(lambda request: httpx.Response(200, json={"success": False, "message": "Token expired"}))
    res = client.get("/api/channel_wise_report/preview-count?from_date=2026-10-24&to_date=2026-10-30",
                     headers={"Authorization": TOKEN})
    assert res.status_code == 502 and res.json()["detail"] == "Token expired"
