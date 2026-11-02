"""Channel-wise report: reports broken down per connected sales channel (Shopify, WooCommerce, ...)."""
import os

from fastapi import APIRouter

from app.core.module_base import BaseModule

from .client import BigshipChannelClient
from .router import router
from .service import Service


class Module(BaseModule):
    name = "channel_wise_report"
    title = "Channel Wise Report"
    description = "Reports broken down per connected sales channel."
    depends_on: list[str] = []

    def get_router(self) -> APIRouter:
        return router

    async def on_startup(self, app) -> None:
        # BIGSHIP_API_BASE_URL in .env overrides `base_url` in modules.yaml.
        base_url = os.getenv("BIGSHIP_API_BASE_URL") or self.config.get("base_url")
        if not base_url:
            raise RuntimeError("channel_wise_report needs BIGSHIP_API_BASE_URL or `base_url` in modules.yaml")
        self.client = BigshipChannelClient(base_url, float(self.config.get("timeout_seconds", 15)))
        app.state.channel_wise_report_service = Service(
            self.client,
            page_size=int(self.config.get("orders_page_size", 1000)),
            max_records=int(self.config.get("orders_max_records", 5000)),
        )
        self.logger.info("started (Bigship API: %s)", base_url)

    async def on_shutdown(self, app) -> None:
        await self.client.close()
