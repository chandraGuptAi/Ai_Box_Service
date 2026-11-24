"""Runtime objects shared by the services: settings, repository, cipher, providers, seller identity."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from app.core.crypto import TokenCipher
from app.core.seller_identity import SellerIdentity, SellerIdentityError

from .config import PROVIDERS, EmailSettings
from .errors import EmailError
from .providers import PROVIDER_CLASSES, BaseEmailProvider
from .repository import EmailRepository


class AccessTokenCache:
    """Short-lived access tokens per connection, in memory only (refresh tokens live encrypted in the DB)."""

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[str, float]] = {}

    def get(self, connection_id: str) -> str | None:
        hit = self._tokens.get(connection_id)
        return hit[0] if hit and hit[1] > time.monotonic() else None

    def put(self, connection_id: str, token: str, expires_in: int) -> None:
        # Renew a minute early so a token never expires mid-send.
        self._tokens[connection_id] = (token, time.monotonic() + max(0, expires_in - 60))

    def drop(self, connection_id: str) -> None:
        self._tokens.pop(connection_id, None)


@dataclass
class EmailContext:
    settings: EmailSettings
    repo: EmailRepository
    cipher: TokenCipher
    http: httpx.AsyncClient
    identity: SellerIdentity
    providers: dict[str, BaseEmailProvider] = field(default_factory=dict)
    access_tokens: AccessTokenCache = field(default_factory=AccessTokenCache)

    @classmethod
    def build(cls, settings: EmailSettings, repo: EmailRepository, provider_transport=None,
              bigship_transport=None) -> "EmailContext":
        http = httpx.AsyncClient(timeout=httpx.Timeout(settings.HTTP_TIMEOUT_SECONDS, connect=10.0),
                                 transport=provider_transport, follow_redirects=False, trust_env=False)
        identity = SellerIdentity(settings.SELLER_IDENTITY, settings.BIGSHIP_API_BASE_URL,
                                  settings.SELLER_IDENTITY_CACHE_SECONDS, transport=bigship_transport)
        cipher = TokenCipher(settings.encryption_keys, settings.TOKEN_ENCRYPTION_ACTIVE_KEY)
        providers = {name: PROVIDER_CLASSES[name](settings, http) for name in PROVIDERS}
        return cls(settings, repo, cipher, http, identity, providers)

    def provider(self, name: str) -> BaseEmailProvider:
        if name not in self.providers:
            raise EmailError("EMAIL_PROVIDER_UNSUPPORTED")
        return self.providers[name]

    def ready_provider(self, name: str) -> BaseEmailProvider:
        provider = self.provider(name)
        if self.settings.provider_problems(name):
            raise EmailError("EMAIL_PROVIDER_NOT_CONFIGURED", provider=name)
        return provider

    async def seller(self, authorization: str | None, x_user_id: str | None) -> str:
        try:
            return await self.identity.resolve(authorization, x_user_id)
        except SellerIdentityError as exc:
            raise EmailError("EMAIL_SELLER_UNAUTHENTICATED" if exc.kind == "unauthenticated"
                             else "EMAIL_IDENTITY_UNAVAILABLE") from None

    async def close(self) -> None:
        await self.identity.close()
        await self.http.aclose()
        await self.repo.pool.close()
