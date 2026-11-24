from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .analysis import Intent

OrderGroup = Literal["ready", "cod", "high_risk", "needs_fixing", "all"]


class AskRequest(BaseModel):
    question: str = Field(..., max_length=300, description="What the seller asked, in English, Hindi or Hinglish")
    channel_id: int | None = Field(None, ge=1, description="Only this store's orders; empty = every store")

    @field_validator("question")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Ask a question about your orders")
        return value


class Metric(BaseModel):
    label: str
    value: str
    tone: Literal["good", "warn", "risk", "info"] = "info"


class Package(BaseModel):
    weight_kg: float | None
    length_cm: float | None
    width_cm: float | None
    height_cm: float | None


class Order(BaseModel):
    """An open store order. No customer name, phone or street address (the pincode is kept for courier rates)."""
    channel_order_id: str
    order_number: str
    order_date: datetime | None
    store: str
    amount: float
    payment: Literal["COD", "Prepaid"]
    items: int
    ready: bool
    high_risk: bool
    reasons: list[str]
    pincode: str
    city: str
    state: str
    total_amount: float
    package: Package


class Action(BaseModel):
    kind: Literal["book", "show", "call"]
    label: str
    group: OrderGroup
    count: int


class InsightResponse(BaseModel):
    intent: Intent
    question: str
    title: str
    answer: str
    ai_written: bool = Field(description="False when the AI was unavailable and the built-in sentence was used")
    metrics: list[Metric]
    #: Order numbers worth looking at first (shown as chips).
    highlight: list[str]
    actions: list[Action]
    follow_ups: list[str]
    #: Open (unshipped) orders the answer is based on, oldest first; at most `orders_returned` of them.
    orders: list[Order]
    open_orders: int
    partial: bool = Field(description="True when the store has more open orders than were read")
    generated_at: datetime
