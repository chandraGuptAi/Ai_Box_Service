"""Which seller is calling: verified with BigShip, never taken from the browser.

BigShip logins are Laravel Sanctum tokens (opaque ``id|secret`` strings, no claims to decode), so the only
way to know whose token it is is to ask BigShip. ``GET /api/get-user-profile`` (``auth:sanctum``) answers
for the token's own account and returns ``data.user_profile_data.cust_id``: the effective seller's
CustomerId (for a sub-user, the parent seller's, so sub-users share the seller's Meta connections).

The X-User-Id header is not used for authorization in this mode: a seller cannot reach another seller's
connections by changing it. Verified mappings are cached by token hash for SELLER_IDENTITY_CACHE_SECONDS;
the token itself is never stored or logged.
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict

import httpx

from app.core.logger import get_logger

from .config import MetaSettings
from .errors import MetaError
from .security import sha256_hex

logger = get_logger("module.meta_integration.identity")

PROFILE_PATH = "/api/get-user-profile"
_BEARER = re.compile(r"^Bearer [A-Za-z0-9|._~+/=-]{8,2048}$")
_SELLER_ID = re.compile(r"^[\x21-\x7e]{1,100}$")
_CACHE_LIMIT = 5000


class SellerIdentity:
    def __init__(self, settings: MetaSettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.mode = settings.SELLER_IDENTITY
        self.ttl = settings.SELLER_IDENTITY_CACHE_SECONDS
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._client = None
        if self.mode == "bigship" and settings.BIGSHIP_API_BASE_URL:
            self._client = httpx.AsyncClient(base_url=settings.BIGSHIP_API_BASE_URL.rstrip("/"),
                                             timeout=httpx.Timeout(settings.HTTP_TIMEOUT_SECONDS, connect=5.0),
                                             transport=transport, follow_redirects=False, trust_env=False)

    async def resolve(self, authorization: str | None, x_user_id: str | None) -> str:
        if self.mode == "header":
            value = (x_user_id or "").strip()
            if not value:
                raise MetaError("META_INVALID_REQUEST", "Authenticated seller is required (X-User-Id).")
            return value
        seller = await self._verified(authorization)
        if x_user_id and x_user_id.strip() != seller:
            # Expected for sub-users (the seller is the parent account); a tampered header has no effect.
            logger.info("X-User-Id differs from the BigShip-verified seller; the verified seller is used")
        return seller

    async def _verified(self, authorization: str | None) -> str:
        value = (authorization or "").strip()
        if not _BEARER.match(value):
            raise MetaError("META_SELLER_UNAUTHENTICATED",
                            "Your BigShip login is missing. Please log in again." if not value else None)
        if self._client is None:
            raise MetaError("META_PLATFORM_NOT_CONFIGURED", "Seller verification is not configured (BIGSHIP_API_BASE_URL).")
        key = sha256_hex(value)
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and hit[1] > now:
            return hit[0]
        seller = await self._ask_bigship(value)
        if self.ttl:
            self._cache[key] = (seller, now + self.ttl)
            self._cache.move_to_end(key)
            while len(self._cache) > _CACHE_LIMIT:
                self._cache.popitem(last=False)
        return seller

    async def _ask_bigship(self, authorization: str) -> str:
        try:
            res = await self._client.get(PROFILE_PATH, headers={"Authorization": authorization,
                                                                 "Accept": "application/json"})
        except httpx.HTTPError as exc:
            logger.warning("BigShip seller verification failed: %s", type(exc).__name__)
            raise MetaError("META_IDENTITY_UNAVAILABLE") from None
        if res.status_code in (401, 403, 404):
            # 401: expired/revoked token. 403: account blocked by BigShip. 404: no seller account behind it.
            raise MetaError("META_SELLER_UNAUTHENTICATED")
        if res.status_code != 200:
            logger.warning("BigShip seller verification returned HTTP %s", res.status_code)
            raise MetaError("META_IDENTITY_UNAVAILABLE")
        try:
            body = res.json()
            if isinstance(body, dict) and body.get("status") in ("Failed", False):
                raise MetaError("META_SELLER_UNAUTHENTICATED")
            profile = body["data"]["user_profile_data"]
            seller = str(profile["cust_id"]).strip()
        except (ValueError, KeyError, TypeError):
            logger.warning("BigShip seller verification: unexpected response shape")
            raise MetaError("META_IDENTITY_UNAVAILABLE") from None
        if not _SELLER_ID.match(seller) or seller.lower() in ("none", "null", "0"):
            raise MetaError("META_SELLER_UNAUTHENTICATED")
        return seller

    def forget(self, authorization: str | None) -> None:
        self._cache.pop(sha256_hex((authorization or "").strip()), None)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
