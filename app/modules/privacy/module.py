"""Privacy filter: removes personal data from text before another service sends it to an AI model."""
from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from app.core.module_base import BaseModule

from .engine import load_detector
from .router import router


class Module(BaseModule):
    name = "privacy"
    title = "Privacy Filter"
    description = "Removes personal data from AI-bound text and reports what was removed. Never blocks."
    depends_on: list[str] = []

    def get_router(self) -> APIRouter:
        return router

    async def on_startup(self, app) -> None:
        # A missing name detector stops startup here instead of letting names through later.
        app.state.privacy_detector = await run_in_threadpool(load_detector)
        self.logger.info("started (local name detector loaded)")
