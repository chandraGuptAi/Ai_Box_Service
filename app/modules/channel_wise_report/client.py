"""HTTP client for Bigship: channel APIs (`/api/channel`, `/api/channel/orders/list`) and the shipment count."""
from typing import Any

import httpx
from fastapi import HTTPException


class BigshipChannelClient:
    def __init__(self, base_url: str, timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    async def list_channels(self, authorization: str) -> list[dict[str, Any]]:
        """Fetch the caller's connected channels using their own Bigship token."""
        return await self._request("GET", "/api/channel", authorization) or []

    async def list_channel_orders(self, authorization: str, channel_id: int | str, status: str,
                                  page: int, per_page: int) -> dict[str, Any]:
        """One page of a channel's store orders: ``{"getOrderList": [...], "totalRecords": n}``."""
        body = {"page": page, "perPage": per_page, "status": status, "channel_id": channel_id}
        return await self._request("POST", "/api/channel/orders/list", authorization, json=body) or {}

    async def count_shipments(self, authorization: str, from_utc: str, to_utc_exclusive: str, status_type: int) -> int:
        """Shipments matching the AI Box filters: `totalRecords` of a one-row /api/get-order-list page.

        Same body the AI Box page used to send from the browser (ShipmentDataService.payload).
        """
        body = {
            "type": status_type, "page": 1, "perPage": 1, "search": "", "search_by_courier": "",
            "search_by_order_id": "", "search_awb_number": "", "search_lrn_number": "", "search_by_cust_id": "",
            "search_by_company_name": "", "search_by_seller_email": "", "only_order_stats": 0, "segment_type": "",
            "fromDate": from_utc, "toDate": to_utc_exclusive,
        }
        data = await self._request("POST", "/api/get-order-list", authorization, json=body) or {}
        try:
            return int(data.get("totalRecords") or 0)
        except (TypeError, ValueError):
            raise HTTPException(502, "Bigship order list returned an invalid totalRecords")

    async def _request(self, method: str, path: str, authorization: str, **kwargs) -> Any:
        """Call Bigship and return the response's ``data``; upstream failures become 401/502/504."""
        try:
            res = await self._client.request(method, path, headers={"Authorization": authorization}, **kwargs)
        except httpx.TimeoutException:
            raise HTTPException(504, "Bigship channel API timed out")
        except httpx.RequestError:
            raise HTTPException(502, "Bigship channel API is unreachable")

        if res.status_code in (401, 403):
            raise HTTPException(401, "Bigship rejected the token")
        if res.status_code >= 400:
            raise HTTPException(502, f"Bigship channel API returned {res.status_code}")

        try:
            body = res.json()
        except ValueError:
            raise HTTPException(502, "Bigship channel API returned invalid JSON")
        # Channel APIs answer {"status": bool}; the order list answers {"success": bool}.
        if not isinstance(body, dict) or body.get("status") is False or body.get("success") is False:
            message = body.get("message") if isinstance(body, dict) else None
            raise HTTPException(502, message or "Bigship API reported a failure")
        return body.get("data")

    async def close(self) -> None:
        await self._client.aclose()
