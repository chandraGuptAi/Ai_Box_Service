"""Which BigShip seller is calling: verified with BigShip, never taken from the browser.

BigShip logins are Laravel Sanctum tokens (opaque ``id|secret``, no claims to decode), so the only way to
know whose token it is is to ask BigShip: ``GET /api/get-user-profile`` answers for the token's own account
with ``data.user_profile_data.cust_id``, the effective seller's CustomerId (for a sub-user, the parent
seller's, so sub-users share the seller's connections).

Modes:
- ``bigship`` (default): verify ``X-Shipment-Authorization`` with BigShip. ``X-User-Id`` is ignored.
- ``header``: trust ``X-User-Id``. Local development only; callers must refuse it in production.

Verified mappings are cached by token hash for ``cache_seconds``; the token itself is never stored or logged.
Errors are ``SellerIdentityError`` with ``kind`` ``unauthenticated`` (log in again) or ``unavailable``
(BigShip unreachable or unconfigured); each module maps them to its own error codes.
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import Literal

import httpx

from app.core.crypto import sha256_hex
from app.core.logger import get_logger

logger = get_logger("core.seller_identity")

PROFILE_PATH = "/api/get-user-profile"
_BEARER = re.compile(r"^Bearer [A-Za-z0-9|._~+/=-]{8,2048}$")
_SELLER_ID = re.compile(r"^[\x21-\x7e]{1,100}$")
_CACHE_LIMIT = 5000


class SellerIdentityError(Exception):
    def __init__(self, kind: Literal["unauthenticated", "unavailable"], message: str = "") -> None:
        self.kind = kind
        super().__init__(message or kind)


class SellerIdentity:
    def __init__(self, mode: str, bigship_base_url: str, cache_seconds: int = 300, timeout_seconds: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.mode = mode
        self.ttl = cache_seconds
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._profiles: OrderedDict[str, tuple[dict, float]] = OrderedDict()
        self._client: httpx.AsyncClient | None = None
        if mode == "bigship" and bigship_base_url:
            self._client = httpx.AsyncClient(base_url=bigship_base_url.rstrip("/"),
                                             timeout=httpx.Timeout(timeout_seconds, connect=5.0),
                                             transport=transport, follow_redirects=False, trust_env=False)

    async def resolve(self, authorization: str | None, x_user_id: str | None) -> str:
        if self.mode == "header":
            value = (x_user_id or "").strip()
            if not _SELLER_ID.match(value):
                raise SellerIdentityError("unauthenticated", "X-User-Id is required")
            return value
        return await self._verified(authorization)

    async def _verified(self, authorization: str | None) -> str:
        value = (authorization or "").strip()
        if not _BEARER.match(value):
            raise SellerIdentityError("unauthenticated", "BigShip login is missing")
        if self._client is None:
            raise SellerIdentityError("unavailable", "BIGSHIP_API_BASE_URL is not configured")
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

    async def profile(self, authorization: str | None) -> dict:
        """The login's own BigShip profile (``data.user_profile_data``: company_name, first_name, ...).

        Always verified with BigShip, whatever the mode. Cached per token like ``resolve``; never logged.
        """
        value = (authorization or "").strip()
        if not _BEARER.match(value):
            raise SellerIdentityError("unauthenticated", "BigShip login is missing")
        if self._client is None:
            raise SellerIdentityError("unavailable", "BIGSHIP_API_BASE_URL is not configured")
        key = sha256_hex(value)
        now = time.monotonic()
        hit = self._profiles.get(key)
        if hit and hit[1] > now:
            return hit[0]
        profile = await self._fetch_profile(value)
        if self.ttl:
            self._profiles[key] = (profile, now + self.ttl)
            self._profiles.move_to_end(key)
            while len(self._profiles) > _CACHE_LIMIT:
                self._profiles.popitem(last=False)
        return profile

    async def _ask_bigship(self, authorization: str) -> str:
        profile = await self._fetch_profile(authorization)
        try:
            seller = str(profile["cust_id"]).strip()
        except KeyError:
            logger.warning("BigShip seller verification: unexpected response shape")
            raise SellerIdentityError("unavailable") from None
        if not _SELLER_ID.match(seller) or seller.lower() in ("none", "null", "0"):
            raise SellerIdentityError("unauthenticated")
        return seller

    async def _fetch_profile(self, authorization: str) -> dict:
        if self._client is None:
            raise SellerIdentityError("unavailable", "BIGSHIP_API_BASE_URL is not configured")
        try:
            res = await self._client.get(PROFILE_PATH, headers={"Authorization": authorization,
                                                                 "Accept": "application/json"})
        except httpx.HTTPError as exc:
            logger.warning("BigShip seller verification failed: %s", type(exc).__name__)
            raise SellerIdentityError("unavailable") from None
        if res.status_code in (401, 403, 404):
            raise SellerIdentityError("unauthenticated")
        if res.status_code != 200:
            logger.warning("BigShip seller verification returned HTTP %s", res.status_code)
            raise SellerIdentityError("unavailable")
        try:
            body = res.json()
            if isinstance(body, dict) and body.get("status") in ("Failed", False):
                raise SellerIdentityError("unauthenticated")
            profile = body["data"]["user_profile_data"]
        except (ValueError, KeyError, TypeError):
            logger.warning("BigShip seller verification: unexpected response shape")
            raise SellerIdentityError("unavailable") from None
        if not isinstance(profile, dict):
            logger.warning("BigShip seller verification: unexpected response shape")
            raise SellerIdentityError("unavailable")
        return profile

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
