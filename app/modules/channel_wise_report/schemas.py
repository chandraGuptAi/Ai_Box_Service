from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel


class Channel(BaseModel):
    channel_id: int | str
    name: str
    display_name: str
    shop_domain: str | None
    platform: str
    connection_status: str
    is_connected: bool
    is_active: bool
    plan_type: str | None
    last_order_sync: datetime | None
    last_inventory_sync: datetime | None
    connected_at: datetime | None


class ChannelSummary(BaseModel):
    total: int
    connected: int
    not_connected: int
    by_platform: dict[str, int]


class ChannelListResponse(BaseModel):
    summary: ChannelSummary
    channels: list[Channel]


class ChannelOrderStatus(str, Enum):
    """Values Bigship's /api/channel/orders/list accepts for `status` (same as the panel's filter)."""
    all = "all"
    unshipped = "Unshipped"
    processing = "Processing"
    shipped = "Shipped"
    cancelled = "Cancelled"
    failed = "Failed"


class ChannelOrder(BaseModel):
    """A store order. Customer name, phone, email and street address are deliberately left out."""
    channel_order_id: int | str
    order_number: str | None
    order_date: datetime | None
    order_status: str | None
    payment_status: str | None
    fulfillment_status: str | None
    payment_mode: str | None
    total_amount: float | None
    collectable_amount: float | None
    items_count: int
    delivery_status: str | None
    delivery_method: str | None
    tracking_number: str | None
    city: str | None
    state: str | None
    pincode: str | None
    country: str | None


class ChannelOrdersSummary(BaseModel):
    total_orders: int
    total_amount: float
    by_order_status: dict[str, int]
    by_payment_status: dict[str, int]
    by_fulfillment_status: dict[str, int]
    by_payment_mode: dict[str, int]


class ChannelOrdersResponse(BaseModel):
    channel_id: int | str
    status: ChannelOrderStatus
    #: Inclusive India (IST) calendar days the orders were filtered to by order_date; null = no date filter.
    from_date: date | None = None
    to_date: date | None = None
    #: totalRecords reported by Bigship for the channel and status, before the date filter.
    total_records: int
    summary: ChannelOrdersSummary
    orders: list[ChannelOrder]


class PreviewCountResponse(BaseModel):
    """How many records an AI Box report with these filters will read."""
    count: int
    #: "shipments" (Bigship order list) or "channel_orders" (one channel's store orders).
    source: str
