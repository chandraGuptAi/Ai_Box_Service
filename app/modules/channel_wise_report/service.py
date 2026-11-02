import json
import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from .client import BigshipChannelClient
from .schemas import (
    Channel,
    ChannelListResponse,
    ChannelOrder,
    ChannelOrdersResponse,
    ChannelOrdersSummary,
    ChannelOrderStatus,
    ChannelSummary,
    PreviewCountResponse,
)

IST = timezone(timedelta(hours=5, minutes=30), "IST")  # India has no DST; avoids needing tzdata on Windows


def display_name(name: str | None) -> str:
    """'nr-store.myshopify.com' -> 'Nr-store', 'https://www.shop.com/' -> 'Shop.com'."""
    if not name:
        return "—"
    if ".myshopify.com" in name.lower():
        name = re.sub(r"\.myshopify\.com.*", "", name, flags=re.I)
    else:
        name = re.sub(r"^https?://(www\.)?", "", name).rstrip("/")
    return name[:1].upper() + name[1:]


def parse_datetime(value: str | None) -> datetime | None:
    """Bigship sends naive UTC timestamps; return them as timezone-aware UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_amount(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def items_count(line_items: Any) -> int:
    """Quantity still to fulfil; all items when everything is fulfilled (same rule as the panel)."""
    if isinstance(line_items, str):
        try:
            line_items = json.loads(line_items)
        except ValueError:
            return 0
    if not isinstance(line_items, list):
        return 0

    def qty(item: dict, key: str) -> int:
        try:
            return int(item.get(key) if item.get(key) is not None else 1)
        except (TypeError, ValueError):
            return 1

    items = [i for i in line_items if isinstance(i, dict)]
    open_items = [i for i in items
                  if qty(i, "fulfillable_quantity" if "fulfillable_quantity" in i else "quantity") > 0
                  and i.get("fulfillment_status") != "fulfilled"]
    return sum(qty(i, "quantity") for i in (open_items or items))


def to_channel(raw: dict[str, Any]) -> Channel:
    status = str(raw.get("Status") or "Unknown")
    return Channel(
        channel_id=raw.get("ChannelId"),
        name=raw.get("ChannelName") or "",
        display_name=display_name(raw.get("ChannelName")),
        shop_domain=raw.get("ShopDomain"),
        platform=raw.get("PlatformType") or "Unknown",
        connection_status=status,
        is_connected=status.lower() == "connected",
        is_active=str(raw.get("IsActive")) == "1",
        plan_type=None if raw.get("shopify_freemium") is None else str(raw["shopify_freemium"]),
        last_order_sync=parse_datetime(raw.get("LastOrderSync")),
        last_inventory_sync=parse_datetime(raw.get("LastInventorySync")),
        connected_at=parse_datetime(raw.get("CreatedAt")),
    )


def to_order(raw: dict[str, Any]) -> ChannelOrder:
    def text(key: str) -> str | None:
        value = raw.get(key)
        return str(value) if value not in (None, "") else None

    global_order = raw.get("global_order") if isinstance(raw.get("global_order"), dict) else {}
    return ChannelOrder(
        channel_order_id=raw.get("ChannelOrderId"),
        order_number=text("ExternalOrderNumber"),
        order_date=parse_datetime(raw.get("OrderDate")),
        order_status=text("OrderStatus"),
        payment_status=text("PaymentStatus"),
        fulfillment_status=text("FulfillmentStatus").capitalize() if text("FulfillmentStatus") else None,
        payment_mode=text("PaymentMode"),
        total_amount=parse_amount(raw.get("TotalAmount")),
        collectable_amount=parse_amount(raw.get("CollectableAmount")),
        items_count=items_count(raw.get("LineItemsJson")),
        delivery_status=text("DeliveryStatus"),
        delivery_method=text("DeliveryMethod"),
        tracking_number=global_order.get("tracking_number") or None,
        city=text("City"),
        state=text("State"),
        pincode=text("Pincode"),
        country=text("Country"),
    )


def summarize_orders(orders: list[ChannelOrder]) -> ChannelOrdersSummary:
    def count(attr: str) -> dict[str, int]:
        return dict(Counter(getattr(o, attr) or "Unknown" for o in orders))

    return ChannelOrdersSummary(
        total_orders=len(orders),
        total_amount=round(sum(o.total_amount or 0 for o in orders), 2),
        by_order_status=count("order_status"),
        by_payment_status=count("payment_status"),
        by_fulfillment_status=count("fulfillment_status"),
        by_payment_mode=count("payment_mode"),
    )


class Service:
    def __init__(self, client: BigshipChannelClient, page_size: int = 1000, max_records: int = 5000) -> None:
        self.client = client
        self.page_size = page_size
        self.max_records = max_records

    async def list_channels(self, authorization: str) -> ChannelListResponse:
        channels = [to_channel(raw) for raw in await self.client.list_channels(authorization)]
        connected = sum(ch.is_connected for ch in channels)
        return ChannelListResponse(
            summary=ChannelSummary(
                total=len(channels),
                connected=connected,
                not_connected=len(channels) - connected,
                by_platform=dict(Counter(ch.platform for ch in channels)),
            ),
            channels=channels,
        )

    async def preview_count(self, authorization: str, from_date: date, to_date: date, status_type: int,
                            channel_id: int | None) -> PreviewCountResponse:
        """Records an AI Box report will read: a channel's orders in the period, else the period's shipments."""
        if channel_id is not None:
            orders = await self.list_channel_orders(authorization, channel_id, ChannelOrderStatus.all,
                                                    from_date, to_date)
            return PreviewCountResponse(count=len(orders.orders), source="channel_orders")
        # Inclusive IST days -> UTC "YYYY-MM-DD HH:MM:SS" boundaries, end exclusive (what the page used to send).
        start = datetime.combine(from_date, time.min, IST).astimezone(timezone.utc)
        end = datetime.combine(to_date + timedelta(days=1), time.min, IST).astimezone(timezone.utc)
        count = await self.client.count_shipments(authorization, start.strftime("%Y-%m-%d %H:%M:%S"),
                                                  end.strftime("%Y-%m-%d %H:%M:%S"), status_type)
        return PreviewCountResponse(count=count, source="shipments")

    async def list_channel_orders(self, authorization: str, channel_id: int | str, status: ChannelOrderStatus,
                                  from_date: date | None = None, to_date: date | None = None) -> ChannelOrdersResponse:
        """Every order of one channel in at most two calls.

        Call 1 (page 1, perPage=page_size) returns totalRecords; when everything fit, that is all.
        Otherwise call 2 asks for page 1 with perPage=totalRecords, which Bigship serves in full.
        """
        async def read(per_page: int) -> dict[str, Any]:
            return await self.client.list_channel_orders(authorization, channel_id, status.value, 1, per_page)

        data = await read(self.page_size)
        total = int(data.get("totalRecords") or 0)
        if total > self.max_records:
            raise HTTPException(413, f"Channel has {total} orders; the limit is {self.max_records}. Narrow the status.")
        if total > self.page_size:
            data = await read(total)
            total = int(data.get("totalRecords") or total)

        # Drop repeated rows defensively so the summary never double-counts an order.
        seen: set[str] = set()
        orders: list[ChannelOrder] = []
        for raw in data.get("getOrderList") or []:
            key = str(raw.get("ChannelOrderId"))
            if key not in seen:
                seen.add(key)
                orders.append(to_order(raw))

        # Bigship cannot filter channel orders by date, so the period is applied here on order_date (IST day).
        if from_date or to_date:
            orders = [o for o in orders if o.order_date and
                      (from_date or date.min) <= o.order_date.astimezone(IST).date() <= (to_date or date.max)]

        return ChannelOrdersResponse(channel_id=channel_id, status=status, from_date=from_date, to_date=to_date,
                                     total_records=total, summary=summarize_orders(orders), orders=orders)
