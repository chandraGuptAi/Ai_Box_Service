"""Email integration settings, read from the environment / .env (prefix ``EMAIL_``).

Missing credentials leave a provider "not configured" (reported by GET /api/email/providers) instead of
failing the service. Nothing here has a usable default for secrets.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import BASE_DIR, get_settings
from app.core.crypto import parse_keys

PROVIDERS = ("gmail", "outlook")


class EmailSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_prefix="EMAIL_", extra="ignore")

    # The AI_python_backend database (same as meta_integration). Tables are prefixed email_.
    DATABASE_URL: str = Field("", validation_alias=AliasChoices("EMAIL_DATABASE_URL", "DATABASE_URL"))
    DB_POOL_MIN: int = 1
    DB_POOL_MAX: int = 5

    # {"1": "<base64 32 bytes>"}; ACTIVE_KEY encrypts, every listed key decrypts. Separate from META_ keys.
    TOKEN_ENCRYPTION_KEYS: str = ""
    TOKEN_ENCRYPTION_ACTIVE_KEY: str = ""

    # Google Cloud console > APIs & Services > Credentials > OAuth client (Web application).
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    # Microsoft Entra admin center > App registrations (accounts in any org + personal Microsoft accounts).
    MICROSOFT_CLIENT_ID: str = ""
    MICROSOFT_CLIENT_SECRET: str = ""
    MICROSOFT_TENANT: str = "common"

    # Registered with both Google and Microsoft: https://<gateway>/api/email/oauth/callback
    OAUTH_REDIRECT_URI: str = ""
    OAUTH_TRANSACTION_TTL_SECONDS: int = Field(default=600, ge=60, le=3600)

    # Shared secret AI_python_backend sends in X-Email-Internal-Key to POST /api/email/internal/draft.
    # The browser never has it, so only the backend (which builds the body and PDF) can create drafts.
    INTERNAL_KEY: str = ""

    MAX_ATTACHMENT_BYTES: int = Field(default=10 * 1024 * 1024, ge=1024)
    MAX_HTML_BYTES: int = Field(default=200_000, ge=1000)
    DRAFTS_PER_HOUR: int = Field(default=60, ge=1)
    HTTP_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0)

    # bigship (default): seller = whoever BigShip says owns X-Shipment-Authorization.
    # header: trust X-User-Id (local development only; refused when ENV=production).
    SELLER_IDENTITY: Literal["bigship", "header"] = "bigship"
    BIGSHIP_API_BASE_URL: str = Field("", validation_alias=AliasChoices("EMAIL_BIGSHIP_API_BASE_URL",
                                                                        "BIGSHIP_API_BASE_URL"))
    SELLER_IDENTITY_CACHE_SECONDS: int = Field(default=300, ge=0, le=3600)

    # ---- derived -----------------------------------------------------------------------------

    @property
    def encryption_keys(self) -> dict[str, bytes]:
        return parse_keys(self.TOKEN_ENCRYPTION_KEYS, "EMAIL_TOKEN_ENCRYPTION_KEYS")

    @property
    def production(self) -> bool:
        return get_settings().ENV == "production"

    def startup_problems(self) -> list[str]:
        """Settings without which the module cannot run at all."""
        problems = []
        if not self.DATABASE_URL:
            problems.append("DATABASE_URL (the AI_python_backend database) is empty")
        try:
            if not self.encryption_keys or self.TOKEN_ENCRYPTION_ACTIVE_KEY not in self.encryption_keys:
                problems.append("EMAIL_TOKEN_ENCRYPTION_KEYS / EMAIL_TOKEN_ENCRYPTION_ACTIVE_KEY are not set")
        except (ValueError, TypeError) as exc:
            problems.append(f"EMAIL_TOKEN_ENCRYPTION_KEYS is invalid ({exc})")
        if self.production and not get_settings().SERVICE_TOKEN:
            problems.append("SERVICE_TOKEN must be set in production")
        return problems

    def identity_problems(self) -> list[str]:
        if self.SELLER_IDENTITY == "header":
            return ["EMAIL_SELLER_IDENTITY (header is not allowed in production)"] if self.production else []
        parsed = urlparse(self.BIGSHIP_API_BASE_URL)
        if not parsed.netloc or parsed.scheme not in ("https", "http") or (
                parsed.scheme == "http" and self.production):
            return ["BIGSHIP_API_BASE_URL"]
        return []

    def provider_problems(self, provider: str) -> list[str]:
        """Missing settings for one provider (empty list = ready to connect)."""
        problems = self.identity_problems()
        if provider == "gmail":
            problems += [n for n, v in (("EMAIL_GOOGLE_CLIENT_ID", self.GOOGLE_CLIENT_ID),
                                        ("EMAIL_GOOGLE_CLIENT_SECRET", self.GOOGLE_CLIENT_SECRET)) if not v]
        elif provider == "outlook":
            problems += [n for n, v in (("EMAIL_MICROSOFT_CLIENT_ID", self.MICROSOFT_CLIENT_ID),
                                        ("EMAIL_MICROSOFT_CLIENT_SECRET", self.MICROSOFT_CLIENT_SECRET)) if not v]
        parsed = urlparse(self.OAUTH_REDIRECT_URI)
        if not parsed.netloc:
            problems.append("EMAIL_OAUTH_REDIRECT_URI")
        elif parsed.scheme != "https" and (self.production or parsed.scheme != "http"):
            problems.append("EMAIL_OAUTH_REDIRECT_URI (https required)")
        return problems


@lru_cache
def get_email_settings() -> EmailSettings:
    return EmailSettings()
