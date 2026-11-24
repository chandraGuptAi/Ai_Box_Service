"""Channel insights: the "AI Insights" box on the panel's Channel Orders page.

The seller asks about their open store orders ("What should I ship today?", "Which orders are high risk?") and
gets the answer with counts, order numbers and next steps. Counts come from Python; the AI (the ai_assistant
module's client) only words them, and never sees customer names, phones or addresses.
Public URL: /api/channel-insights/* through the gateway.
"""
import os

from fastapi import APIRouter

from app.core.module_base import BaseModule
from app.modules.channel_wise_report.client import BigshipChannelClient

from .analysis import Rules
from .router import router
from .service import Service

# Tests replace this with an httpx.MockTransport.
BIGSHIP_TRANSPORT = None


class Module(BaseModule):
    name = "channel_insights"
    title = "Channel Insights"
    description = "Answers questions about open store orders: what to ship, what is risky, what COD costs."
    depends_on: list[str] = ["ai_assistant"]

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
        app.state.channel_insights_service = Service(
            self.client, rules, page_size=int(self.config.get("orders_page_size", 1000)),
            max_records=int(self.config.get("orders_max_records", 5000)))
        self.logger.info("started (Bigship API: %s)", base_url)

    async def on_shutdown(self, app) -> None:
        app.state.channel_insights_service = None
        await self.client.close()
