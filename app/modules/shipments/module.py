"""Shipments: complete Bigship order-list fetch, personal data removed at the source, short-lived cache."""
import os

from fastapi import APIRouter

from app.core.module_base import BaseModule

from .cache import FetchCache
from .fetch import Limits, ShipmentFetcher
from .router import router


class Module(BaseModule):
    name = "shipments"
    title = "Shipments"
    description = "All shipments of a period for reports, without personal data."
    depends_on: list[str] = []

    def get_router(self) -> APIRouter:
        return router

    async def on_startup(self, app) -> None:
        # BIGSHIP_SHIPMENT_API_BASE_URL (or BIGSHIP_API_BASE_URL) in .env overrides `base_url` in modules.yaml.
        base_url = (os.getenv("BIGSHIP_SHIPMENT_API_BASE_URL") or os.getenv("BIGSHIP_API_BASE_URL")
                    or self.config.get("base_url"))
        if not base_url:
            raise RuntimeError("shipments needs BIGSHIP_SHIPMENT_API_BASE_URL or `base_url` in modules.yaml")
        defaults = Limits()
        limits = Limits(**{name: type(getattr(defaults, name))(self.config[name])
                           for name in defaults.__dataclass_fields__ if name in self.config})
        self.fetcher = ShipmentFetcher(base_url, limits)
        app.state.shipments_fetcher = self.fetcher
        app.state.shipments_cache = FetchCache(float(self.config.get("cache_ttl_seconds", 600)),
                                               int(self.config.get("cache_max_entries", 50)))
        self.logger.info("started (Bigship API: %s)", base_url)

    async def on_shutdown(self, app) -> None:
        await self.fetcher.close()
