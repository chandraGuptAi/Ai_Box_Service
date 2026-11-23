"""Runtime objects shared by the services: settings, repository, cipher, Graph client, providers."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from uuid import UUID

from .config import PLATFORMS, MetaSettings
from .errors import MetaError
from .providers import PROVIDER_CLASSES, BaseMetaProvider, GraphClient
from .repository import MetaRepository
from .security import TokenCipher


class SellerRateLimiter:
    """Sliding one-minute window per (seller, action). Process-local; the API runs one worker."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def check(self, seller_id: str, action: str) -> None:
        now = time.monotonic()
        hits = self._hits[(seller_id, action)]
        while hits and hits[0] < now - 60:
            hits.popleft()
        if len(hits) >= self.per_minute:
            raise MetaError("META_SELLER_RATE_LIMITED", retry_after=max(1, int(60 - (now - hits[0]))))
        hits.append(now)


@dataclass
class MetaContext:
    settings: MetaSettings
    repo: MetaRepository
    cipher: TokenCipher
    graph: GraphClient
    providers: dict[str, BaseMetaProvider] = field(default_factory=dict)
    limiter: SellerRateLimiter | None = None

    @classmethod
    def build(cls, settings: MetaSettings, repo: MetaRepository, graph: GraphClient) -> "MetaContext":
        cipher = TokenCipher(settings.encryption_keys, settings.TOKEN_ENCRYPTION_ACTIVE_KEY)
        providers = {name: PROVIDER_CLASSES[name](graph, settings) for name in PLATFORMS}
        return cls(settings, repo, cipher, graph, providers, SellerRateLimiter(settings.SELLER_RATE_LIMIT_PER_MINUTE))

    def provider(self, platform: str) -> BaseMetaProvider:
        if platform not in self.providers:
            raise MetaError("META_PLATFORM_UNSUPPORTED")
        return self.providers[platform]

    def ensure_platform(self, platform: str) -> BaseMetaProvider:
        provider = self.provider(platform)
        if platform not in self.settings.ENABLED_PLATFORMS or self.settings.platform_problems(platform):
            raise MetaError("META_PLATFORM_NOT_CONFIGURED", platform=platform)
        return provider

    # Tokens are encrypted with the connection/transaction ID as associated data.
    def encrypt(self, token: str, aad: str) -> tuple[bytes, str]:
        return self.cipher.encrypt(token, aad)

    async def token_for(self, connection_id: UUID, platform: str) -> str:
        cred = await self.repo.get_credential(connection_id)
        if cred is None:
            raise MetaError("META_RECONNECT_REQUIRED", platform=platform)
        return self.cipher.decrypt(cred["encrypted_access_token"], cred["key_version"], str(connection_id))
