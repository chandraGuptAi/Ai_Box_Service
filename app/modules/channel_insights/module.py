"""Channel insights: the "AI Insights" box on the panel's Channel Orders page.

The seller asks about their open store orders ("What should I ship today?", "Which orders are high risk?") and
gets the answer with counts, order numbers and next steps. Counts come from Python; the AI (the ai_assistant
module's client) only words them, and never sees customer names, phones or addresses.
Public URL: /api/channel-insights/* through the gateway.
"""
import os
from pathlib import Path

from fastapi import APIRouter

from app.core.module_base import BaseModule
from app.core.pg import migrate, open_pool
from app.modules.channel_wise_report.client import BigshipChannelClient

from .analysis import Rules
from .config import InsightSettings
from .router import router
from .service import Service
from .store import SuggestionStore

# Tests replace this with an httpx.MockTransport.
BIGSHIP_TRANSPORT = None
# Tests set a fake store here instead of a database.
SUGGESTION_STORE = None

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_LOCK_KEY = 0x43494E53  # "CINS"


class Module(BaseModule):
    name = "channel_insights"
    title = "Channel Insights"
    description = "Answers questions about open store orders: what to ship, what is risky, what COD costs."
    depends_on: list[str] = ["ai_assistant"]
    store = None

    def get_router(self) -> APIRouter:
        return router

    async def on_startup(self, app) -> None:
        base_url = os.getenv("BIGSHIP_API_BASE_URL") or self.config.get("base_url")
        if not base_url:
            raise RuntimeError("channel_insights needs BIGSHIP_API_BASE_URL or `base_url` in modules.yaml")
        self.client = BigshipChannelClient(base_url, float(self.config.get("timeout_seconds", 20)),
                                           transport=BIGSHIP_TRANSPORT)
        defaults = Rules()
        rules = Rules(**{key: float(self.config.get(key, getattr(defaults, key)))
                         for key in ("high_value_cod", "rto_rate", "confirm_reduction", "rto_cost")})
        service = Service(self.client, rules, page_size=int(self.config.get("orders_page_size", 1000)),
                          max_records=int(self.config.get("orders_max_records", 5000)))
        self.store = await self._suggestion_store()
        service.suggestion_store = self.store
        app.state.channel_insights_service = service
        self.logger.info("started (Bigship API: %s, suggestions from %s)", base_url,
                         "the database" if self.store else "the fixed list")

    async def _suggestion_store(self):
        """Chips from channel_insight_suggestions; None (fixed list) without a database."""
        if SUGGESTION_STORE is not None:
            return SUGGESTION_STORE
        dsn = InsightSettings().DATABASE_URL.strip()
        if not dsn:
            return None
        try:
            pool = await open_pool(dsn, "channel_insights", 1, 2)
        except Exception as exc:   # noqa: BLE001 - a DB outage must not stop the box
            self.logger.error("Suggestions use the fixed list: database unavailable (%s)", type(exc).__name__)
            return None
        try:
            async with pool.connection() as conn:
                await migrate(conn, MIGRATIONS_DIR, "channel_insights_schema_migrations", _LOCK_KEY)
        except Exception as exc:   # noqa: BLE001
            await pool.close()
            self.logger.error("Suggestions use the fixed list: migration failed (%s)", type(exc).__name__)
            return None
        return SuggestionStore(pool, float(self.config.get("suggestions_cache_seconds", 60)))

    async def on_shutdown(self, app) -> None:
        app.state.channel_insights_service = None
        await self.client.close()
        if self.store is not None and SUGGESTION_STORE is None:
            await self.store.close()
