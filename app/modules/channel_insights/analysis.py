"""Facts about the seller's open store orders: what can ship, what needs a call, what is risky, what COD costs.

Everything here is plain Python so the numbers are exact; the AI only puts them into words.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from app.modules.channel_wise_report.service import IST, display_name, items_count, parse_amount, parse_datetime

Intent = Literal["ship", "risk", "cod", "summary", "other"]

#: Payment / order states that mean the order will not be shipped.
_DEAD = {"cancelled", "voided", "refunded", "failed"}
#: Prepaid orders whose money has arrived.
_PAID = {"paid", "partially paid"}

# Words (English, Hinglish, Hindi) that pick the suggested questions; checked in this order.
_INTENT_WORDS: tuple[tuple[Intent, tuple[str, ...]], ...] = (
    ("cod", ("cod", "save", "saving", "bach", "confirm", "बच", "कैश")),
    ("risk", ("risk", "rto", "fraud", "fake", "khatra", "jokhim", "जोखिम", "खतर")),
    ("ship", ("ship", "dispatch", "bhej", "book", "courier", "भेज", "शिप")),
    ("summary", ("summar", "overview", "today", "aaj", "report", "आज", "सारांश")),
)


@dataclass(frozen=True)
class Rules:
    high_value_cod: float = 3000.0   # COD orders at or above this are high risk
    rto_rate: float = 0.25           # share of unconfirmed COD orders that come back (estimate)
    confirm_reduction: float = 0.5   # how much a confirmation call cuts that share (estimate)
    rto_cost: float = 150.0          # forward + return shipping lost on one returned order, in rupees (estimate)


@dataclass
class OpenOrder:
    channel_order_id: str
    order_number: str
    order_date: datetime | None
    store: str
    amount: float
    cod: bool
    paid: bool
    items: int
    reasons: list[str] = field(default_factory=list)
    # For courier rates (frontend only, never sent to the AI): delivery pincode and the package.
    pincode: str = ""
    city: str = ""
    state: str = ""
    total_amount: float = 0.0
    weight_kg: float | None = None
    length_cm: float | None = None
    width_cm: float | None = None
    height_cm: float | None = None

    @property
    def high_risk(self) -> bool:
        return self.cod and bool(self.reasons)

    @property
    def ready(self) -> bool:
        return not self.cod and self.paid and not self.reasons


def detect_intent(question: str) -> Intent:
    text = question.lower()
    for intent, words in _INTENT_WORDS:
        if any(word in text for word in words):
            return intent
    return "other"


def _text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    return str(value).strip() if value not in (None, "") else ""


def _positive(value: Any) -> float | None:
    amount = parse_amount(value)
    return amount if amount and amount > 0 else None


def to_open_order(raw: dict[str, Any], rules: Rules) -> OpenOrder | None:
    """None for orders that will not ship (cancelled, voided, refunded, failed)."""
    states = {_text(raw, "OrderStatus").lower(), _text(raw, "PaymentStatus").lower()}
    if states & _DEAD:
        return None
    mode = _text(raw, "PaymentMode").upper()
    cod = mode in ("COD", "CASH ON DELIVERY")
    amount = parse_amount(raw.get("CollectableAmount") if cod else None) or parse_amount(raw.get("TotalAmount")) or 0.0

    reasons: list[str] = []
    if not (_text(raw, "ShippingAddress1") and _text(raw, "Pincode")):
        reasons.append("incomplete address")
    if not _text(raw, "CustomerPhone"):
        reasons.append("no phone number")
    if cod and amount >= rules.high_value_cod:
        reasons.append(f"high COD value (₹{amount:,.0f})")
    if not cod and _text(raw, "PaymentStatus").lower() not in _PAID:
        reasons.append("payment not received")

    number = _text(raw, "ExternalOrderNumber") or str(raw.get("ChannelOrderId"))
    channel = raw.get("channel") if isinstance(raw.get("channel"), dict) else {}
    return OpenOrder(
        channel_order_id=str(raw.get("ChannelOrderId")),
        order_number=number if number.startswith("#") else f"#{number}",
        order_date=parse_datetime(raw.get("OrderDate")),
        store=display_name(channel.get("ChannelName")) if channel.get("ChannelName") else "—",
        amount=round(amount, 2),
        cod=cod,
        paid=_text(raw, "PaymentStatus").lower() in _PAID,
        items=items_count(raw.get("LineItemsJson")),
        reasons=reasons,
        pincode=_text(raw, "Pincode"),
        city=_text(raw, "City"),
        state=_text(raw, "State"),
        total_amount=parse_amount(raw.get("TotalAmount")) or 0.0,
        weight_kg=_positive(raw.get("PackageWeight")),
        length_cm=_positive(raw.get("PackageLength")),
        width_cm=_positive(raw.get("PackageWidth")),
        height_cm=_positive(raw.get("PackageHeight")),
    )


@dataclass
class Facts:
    orders: list[OpenOrder]
    rules: Rules
    now: datetime

    @property
    def ready(self) -> list[OpenOrder]:
        return [o for o in self.orders if o.ready]

    @property
    def cod(self) -> list[OpenOrder]:
        return [o for o in self.orders if o.cod]

    @property
    def high_risk(self) -> list[OpenOrder]:
        return [o for o in self.orders if o.high_risk]

    @property
    def needs_fixing(self) -> list[OpenOrder]:
        """Prepaid orders that cannot ship yet: address, phone or payment missing."""
        return [o for o in self.orders if not o.cod and o.reasons]

    @property
    def today(self) -> list[OpenOrder]:
        day = self.now.astimezone(IST).date()
        return [o for o in self.orders if o.order_date and o.order_date.astimezone(IST).date() == day]

    def cod_saving(self) -> dict[str, float]:
        """Estimated return-shipping money saved by confirming every open COD order."""
        cod = self.cod
        returns_avoided = len(cod) * self.rules.rto_rate * self.rules.confirm_reduction
        return {
            "cod_orders": len(cod),
            "cod_value": round(sum(o.amount for o in cod), 2),
            "returns_avoided": round(returns_avoided, 1),
            "saving": round(returns_avoided * self.rules.rto_cost),
            "rto_rate_percent": round(self.rules.rto_rate * 100),
            "confirm_reduction_percent": round(self.rules.confirm_reduction * 100),
            "rto_cost": self.rules.rto_cost,
        }

    def numbers(self) -> dict[str, Any]:
        """The numbers the AI may use (no customer details)."""
        reasons: dict[str, int] = {}
        for order in self.orders:
            for reason in order.reasons:
                key = "high COD value" if reason.startswith("high COD value") else reason
                reasons[key] = reasons.get(key, 0) + 1
        return {
            "open_orders": len(self.orders),
            "open_value": round(sum(o.amount for o in self.orders), 2),
            "ready_to_ship": len(self.ready),
            "cod_orders_needing_a_call": len(self.cod),
            "high_risk_cod_orders": len(self.high_risk),
            "prepaid_orders_needing_fixes": len(self.needs_fixing),
            "open_orders_placed_today": len(self.today),
            "problems": reasons,
            "high_value_cod_from": self.rules.high_value_cod,
            "cod_saving_estimate": self.cod_saving(),
        }


def rupees(value: float) -> str:
    """Indian grouping: 167000 -> '1,67,000'."""
    whole = f"{round(value):d}"
    sign, whole = ("-", whole[1:]) if whole.startswith("-") else ("", whole)
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    return f"{sign}₹{whole}"
