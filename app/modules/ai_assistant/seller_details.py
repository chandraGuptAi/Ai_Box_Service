"""Fills the seller's own details into an answer, after the AI has written it.

The AI writes fixed placeholders ([shop name], [your name], [your phone], [your email], [your website]) and
never sees the real values: they come from the logged-in seller's BigShip profile and are put in here.
Customer placeholders ([customer name], [order number]...) and anything unknown are left as they are.
"""
from __future__ import annotations

import re
from typing import Any

# Placeholder text (lower case, single spaces) -> which seller detail it means. Also covers the usual ways a
# user types them in a request ("Write ... for my shop [shop name]").
_PLACEHOLDERS = {
    "shop": ("shop name", "your shop name", "business name", "your business name", "company name",
             "your company name", "store name", "your store name", "brand name", "your brand name"),
    "name": ("your name", "your full name", "sender name"),
    "phone": ("your phone", "your phone number", "your mobile number", "your contact number", "shop phone",
              "business phone", "support phone", "support number"),
    "email": ("your email", "your email address", "shop email", "business email", "support email"),
    "website": ("your website", "website", "website link", "shop website", "store link", "website url"),
}
_LOOKUP = {text: detail for detail, texts in _PLACEHOLDERS.items() for text in texts}
_PLACEHOLDER = re.compile(r"\[([^\[\]\n]{2,40})\]")
_EMPTY = {"", "none", "null", "undefined", "na", "n/a", "-"}


def _clean(value: Any) -> str:
    text = " ".join(str(value).split()) if value is not None else ""
    return "" if text.lower() in _EMPTY else text[:120]


def seller_values(profile: dict) -> dict[str, str]:
    """The details a seller would sign with, from BigShip ``user_profile_data``; missing ones are left out."""
    first = lambda *keys: next((v for v in (_clean(profile.get(k)) for k in keys) if v), "")  # noqa: E731
    values = {
        "shop": first("company_name"),
        "name": " ".join(filter(None, (_clean(profile.get("first_name")), _clean(profile.get("last_name"))))),
        "phone": first("company_mobile_no", "mobile_no"),
        "email": first("company_email_id", "email_id"),
        "website": first("company_website_url"),
    }
    return {detail: value for detail, value in values.items() if value}


def fill(text: str, values: dict[str, str]) -> str:
    if not values:
        return text

    def replace(match: re.Match) -> str:
        detail = _LOOKUP.get(" ".join(match.group(1).lower().split()))
        return values.get(detail, match.group(0)) if detail else match.group(0)

    return _PLACEHOLDER.sub(replace, text)
