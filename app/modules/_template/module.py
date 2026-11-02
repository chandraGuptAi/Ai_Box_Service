"""__MODULE_NAME__ module."""
from fastapi import APIRouter

from app.core.module_base import BaseModule

from .router import router
from .service import Service


class Module(BaseModule):
    name = "__MODULE_NAME__"
    description = "TODO: describe what this module does."
    depends_on: list[str] = []

    def get_router(self) -> APIRouter:
        return router

    async def on_startup(self, app) -> None:
        app.state.__MODULE_NAME___service = Service(self.config)
        self.logger.info("started")

    async def on_shutdown(self, app) -> None:
        self.logger.info("stopped")
