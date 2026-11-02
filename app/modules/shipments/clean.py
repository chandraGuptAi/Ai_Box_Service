"""Personal data leaves the shipment records here, before they go anywhere else.

Every field whose key names a person, contact detail, address, identity/tax document or file link is
removed at any depth (consignor and consignee blocks keep only city, state, pin, country and business
type). Each removed field is reported by path and reason, never by value. Report fields (status,
courier, dates, amounts, weight, city/state/pin, lane, products) stay, and the lane is split into
origin and destination zones.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

# Keys ending in "name" that are not about a person or business.
_PUBLIC_NAMES = ("courier", "carrier", "product", "item", "channel", "platform", "store", "status", "lsp",
                 "partner", "zone", "lane", "segment", "service", "payment", "category")
_RULES = (
    ("contact detail", re.compile(r"email|mobile|phone|telephone|whatsapp|contactno|contactnumber")),
    ("address", re.compile(r"address|landmark|street|houseno|flatno")),
    ("identity or tax document", re.compile(r"^pan|aadha+r|gstin|gstno|gstnumber|kyc|passport|voterid|dob$|"
                                            r"dateofbirth|signature|ewaybillnumber")),
    ("bank detail", re.compile(r"bank|accountnumber|ifsc|upi")),
    ("file link", re.compile(r"url$|link$")),
)


def _norm(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _reason(key: str) -> str | None:
    compact = _norm(key)
    for reason, pattern in _RULES:
        if pattern.search(compact):
            return reason
    if compact.endswith("name") and not compact.startswith(_PUBLIC_NAMES):
        return "person or business name"
    return None


def lane_zones(lane: Any) -> tuple[str | None, str | None]:
    """("N1", "E2") from lane "N1-E2"; (None, None) when the lane is not "<origin>-<destination>"."""
    match = re.fullmatch(r"\s*([^-]+?)\s*-\s*([^-]+?)\s*", str(lane or ""))
    return (match[1], match[2]) if match else (None, None)


def _strip(value: Any, path: str, removed: Counter) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            where = f"{path}.{key}" if path else str(key)
            reason = _reason(key)
            if reason and item not in (None, ""):
                removed[(re.sub(r"\.\d+(?=\.|$)", "[]", where), reason)] += 1
            elif not reason:
                out[key] = _strip(item, where, removed)
        return out
    if isinstance(value, list):
        return [_strip(item, f"{path}.{i}", removed) for i, item in enumerate(value)]
    return value


def clean_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(rows without personal data, [{"field", "reason", "count"}]) for the removed fields."""
    removed: Counter = Counter()
    out = []
    for row in rows:
        clean = _strip(row, "", removed)
        lane = next((v for k, v in clean.items() if _norm(k) == "lane"), None)
        origin, destination = lane_zones(lane)
        if origin:
            clean.setdefault("origin_zone", origin)
            clean.setdefault("destination_zone", destination)
        out.append(clean)
    return out, [{"field": f, "reason": r, "count": n} for (f, r), n in sorted(removed.items())]
