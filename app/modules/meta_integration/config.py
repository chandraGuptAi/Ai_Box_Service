"""Meta integration settings, read from the environment / .env (prefix ``META_``).

Nothing here has a usable default for credentials: a missing value leaves the affected platform
"not configured" (reported by GET /api/meta/platforms) instead of failing the whole service.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import BASE_DIR

PLATFORMS = ("whatsapp", "instagram", "facebook")
_VERSION = re.compile(r"^v\d{2,3}\.0$")


class MetaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_prefix="META_", extra="ignore")

    # The same PostgreSQL database AI_python_backend uses: set DATABASE_URL to the same value as
    # there (META_DATABASE_URL overrides it). All tables are prefixed meta_ and created by this
    # module's own migrations. Empty = module runs in "not configured" mode.
    DATABASE_URL: str = Field("", validation_alias=AliasChoices("META_DATABASE_URL", "DATABASE_URL"))
    DB_POOL_MIN: int = 1
    DB_POOL_MAX: int = 5

    APP_ID: str = ""
    APP_SECRET: str = ""
    # Required, e.g. v27.0. No default on purpose: pin a version you have verified is supported.
    GRAPH_API_VERSION: str = ""
    WEBHOOK_VERIFY_TOKEN: str = ""
    # Server callback registered in the Meta app (Facebook Login for Business + Instagram Login).
    OAUTH_REDIRECT_URI: str = ""
    # Angular page the callback returns the browser to, e.g. https://panel.example/ai_box/create
    FRONTEND_RETURN_URL: str = ""
    WEBHOOK_CALLBACK_URL: str = ""
    # Public URL of /api/meta as Meta and browsers reach it (through the gateway), e.g.
    # https://ai.example.com/api/meta. Used for the data-deletion status link.
    PUBLIC_API_BASE_URL: str = ""

    # WhatsApp Embedded Signup v4: Facebook Login for Business configuration ID.
    WHATSAPP_CONFIG_ID: str = ""
    # Facebook Pages: Facebook Login for Business configuration ID.
    FACEBOOK_CONFIG_ID: str = ""
    # Send PKCE (S256) on the Facebook Login dialog. Enable only after verifying it on your app.
    FACEBOOK_PKCE: bool = False
    # Instagram through Facebook Login for Business (Unified login): a configuration with Instagram
    # assets on the main app. When set, Instagram uses META_APP_ID/SECRET and graph.facebook.com.
    INSTAGRAM_CONFIG_ID: str = ""
    # Instagram API with Instagram Login uses its own app ID/secret (App Dashboard > Instagram).
    # Used only when META_INSTAGRAM_CONFIG_ID is empty.
    INSTAGRAM_APP_ID: str = ""
    INSTAGRAM_APP_SECRET: str = ""

    ENABLED_PLATFORMS: list[str] = Field(default_factory=lambda: list(PLATFORMS))

    # {"1": "<base64 32-byte key>", "2": "..."}; ACTIVE_KEY encrypts, every listed key decrypts.
    TOKEN_ENCRYPTION_KEYS: str = ""
    TOKEN_ENCRYPTION_ACTIVE_KEY: str = ""

    SYNC_ENABLED: bool = True
    SYNC_INTERVAL_MINUTES: int = Field(default=30, ge=5)
    INITIAL_SYNC_DAYS: int = Field(default=30, ge=1, le=89)
    INCREMENTAL_SYNC_DAYS: int = Field(default=3, ge=1, le=30)
    MANUAL_SYNC_MIN_INTERVAL_SECONDS: int = Field(default=300, ge=0)
    SYNC_MAX_RETRIES: int = Field(default=5, ge=0, le=10)
    SYNC_LEASE_SECONDS: int = Field(default=900, ge=60)
    # Run the job/webhook worker inside the API process. Set false when a separate worker runs.
    WORKER_ENABLED: bool = True
    WORKER_POLL_SECONDS: float = Field(default=5.0, gt=0)
    # A connection whose last successful sync is older than this is reported as stale.
    STALE_AFTER_MINUTES: int = Field(default=180, ge=10)

    HTTP_TIMEOUT_SECONDS: float = Field(default=20.0, gt=0)
    OAUTH_TRANSACTION_TTL_SECONDS: int = Field(default=600, ge=60, le=3600)
    WEBHOOK_RETENTION_DAYS: int = Field(default=30, ge=1)
    AUDIT_RETENTION_DAYS: int = Field(default=365, ge=30)
    # Two sellers may connect the same Meta account only when this is true (agency setups).
    ALLOW_SHARED_ACCOUNTS: bool = False
    # Requests per minute per seller for connect/sync endpoints.
    SELLER_RATE_LIMIT_PER_MINUTE: int = Field(default=20, ge=1)
    # X-Meta-Admin-Key for /api/meta/admin/*. Empty = admin API disabled.
    ADMIN_API_KEY: str = ""

    # How seller routes learn which seller is calling:
    #   bigship (default): the caller's own BigShip login token (X-Shipment-Authorization) is checked with
    #     BigShip GET /api/get-user-profile and the seller is that profile's cust_id. X-User-Id is ignored.
    #   header: trust X-User-Id. Local development only; refused when ENV=production.
    SELLER_IDENTITY: Literal["bigship", "header"] = "bigship"
    # The same BigShip API the channel_wise_report and shipments modules call.
    BIGSHIP_API_BASE_URL: str = Field("", validation_alias=AliasChoices("META_BIGSHIP_API_BASE_URL",
                                                                        "BIGSHIP_API_BASE_URL"))
    # How long a verified BigShip token -> seller mapping is reused (0 = verify every request).
    SELLER_IDENTITY_CACHE_SECONDS: int = Field(default=300, ge=0, le=3600)

    @field_validator("ENABLED_PLATFORMS")
    @classmethod
    def _known_platforms(cls, value: list[str]) -> list[str]:
        unknown = set(value) - set(PLATFORMS)
        if unknown:
            raise ValueError(f"unknown Meta platforms: {sorted(unknown)}")
        return value

    # ---- derived -----------------------------------------------------------------------------

    @property
    def instagram_uses_facebook_login(self) -> bool:
        return bool(self.INSTAGRAM_CONFIG_ID.strip())

    @property
    def database_configured(self) -> bool:
        return bool(self.DATABASE_URL)

    @property
    def encryption_keys(self) -> dict[str, bytes]:
        if not self.TOKEN_ENCRYPTION_KEYS:
            return {}
        raw = json.loads(self.TOKEN_ENCRYPTION_KEYS)
        keys = {str(k): base64.b64decode(v) for k, v in raw.items()}
        for version, key in keys.items():
            if len(key) != 32:
                raise ValueError(f"META_TOKEN_ENCRYPTION_KEYS[{version}] must decode to 32 bytes")
        return keys

    def platform_problems(self, platform: str) -> list[str]:
        """Missing/invalid settings for one platform (empty list = configured)."""
        problems: list[str] = []
        if not self.database_configured:
            problems.append("DATABASE_URL")
        if not _VERSION.match(self.GRAPH_API_VERSION):
            problems.append("META_GRAPH_API_VERSION")
        try:
            if not self.encryption_keys or self.TOKEN_ENCRYPTION_ACTIVE_KEY not in self.encryption_keys:
                problems.append("META_TOKEN_ENCRYPTION_KEYS/ACTIVE_KEY")
        except (ValueError, TypeError):
            problems.append("META_TOKEN_ENCRYPTION_KEYS (invalid)")
        problems += self.identity_problems()
        if platform == "whatsapp":
            problems += [n for n, v in (("META_APP_ID", self.APP_ID), ("META_APP_SECRET", self.APP_SECRET),
                                        ("META_WHATSAPP_CONFIG_ID", self.WHATSAPP_CONFIG_ID)) if not v]
        elif platform == "facebook":
            problems += [n for n, v in (("META_APP_ID", self.APP_ID), ("META_APP_SECRET", self.APP_SECRET),
                                        ("META_FACEBOOK_CONFIG_ID", self.FACEBOOK_CONFIG_ID)) if not v]
            problems += self._redirect_problems()
        elif platform == "instagram":
            needed = ((("META_APP_ID", self.APP_ID), ("META_APP_SECRET", self.APP_SECRET))
                      if self.instagram_uses_facebook_login else
                      (("META_INSTAGRAM_APP_ID", self.INSTAGRAM_APP_ID),
                       ("META_INSTAGRAM_APP_SECRET", self.INSTAGRAM_APP_SECRET)))
            problems += [n for n, v in needed if not v]
            problems += self._redirect_problems()
        return problems

    def identity_problems(self) -> list[str]:
        """Seller identity must be verifiable: never a browser-supplied ID in production."""
        if self.SELLER_IDENTITY == "header":
            return [] if self.allow_http_urls else ["META_SELLER_IDENTITY (header is not allowed in production)"]
        parsed = urlparse(self.BIGSHIP_API_BASE_URL)
        if not parsed.netloc or parsed.scheme not in ("https", "http") or (
                parsed.scheme == "http" and not self.allow_http_urls):
            return ["BIGSHIP_API_BASE_URL"]
        return []

    def _redirect_problems(self) -> list[str]:
        problems = []
        for name, url in (("META_OAUTH_REDIRECT_URI", self.OAUTH_REDIRECT_URI),
                          ("META_FRONTEND_RETURN_URL", self.FRONTEND_RETURN_URL)):
            parsed = urlparse(url)
            if not url or not parsed.netloc:
                problems.append(name)
            elif parsed.scheme != "https" and not (self.allow_http_urls and parsed.scheme == "http"):
                problems.append(f"{name} (https required)")
        return problems

    @property
    def allow_http_urls(self) -> bool:
        from app.core.config import get_settings
        return get_settings().ENV != "production"

    def webhook_configured(self) -> bool:
        return bool(self.database_configured and self.APP_SECRET and self.WEBHOOK_VERIFY_TOKEN)


@dataclass(frozen=True)
class GraphHosts:
    facebook: str = "https://graph.facebook.com"
    instagram: str = "https://graph.instagram.com"
    instagram_oauth: str = "https://api.instagram.com"
    instagram_authorize: str = "https://www.instagram.com/oauth/authorize"
    facebook_dialog: str = "https://www.facebook.com"


@lru_cache
def get_meta_settings() -> MetaSettings:
    return MetaSettings()
