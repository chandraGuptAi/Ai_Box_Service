"""The suggested-question chips, from the channel_insight_suggestions table (cached for a minute)."""
import time

from app.core.pg import AsyncPool


class SuggestionStore:
    def __init__(self, pool: AsyncPool, cache_seconds: float = 60) -> None:
        self.pool, self.cache_seconds = pool, cache_seconds
        self._cached: list[str] | None = None
        self._at = 0.0

    async def list(self) -> list[str]:
        if self._cached is not None and time.monotonic() - self._at < self.cache_seconds:
            return self._cached
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT question FROM channel_insight_suggestions WHERE active ORDER BY sort_order, id")).fetchall()
        self._cached = [r["question"].strip() for r in rows if r["question"].strip()]
        self._at = time.monotonic()
        return self._cached

    async def close(self) -> None:
        await self.pool.close()
