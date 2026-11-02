"""Report memory: reuse plans of questions that worked, and show the planner similar successful examples."""
import os
from pathlib import Path

from fastapi import APIRouter

from app.core.config import BASE_DIR
from app.core.module_base import BaseModule

from .embed import Embedder
from .router import router
from .store import MemoryStore


class Module(BaseModule):
    name = "report_memory"
    title = "Report Memory"
    description = "Learns from generated reports and user feedback; finds similar past questions (Hindi/English)."
    depends_on: list[str] = []

    def get_router(self) -> APIRouter:
        return router

    async def on_startup(self, app) -> None:
        # REPORT_MEMORY_SEMANTIC=false keeps spelling similarity only (no model download or extra RAM).
        semantic = os.getenv("REPORT_MEMORY_SEMANTIC", str(self.config.get("semantic", True))).strip().lower() \
            not in {"0", "false", "no", "off"}
        embedder = Embedder(self.config.get("model", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"),
                            semantic=semantic, threads=int(self.config.get("threads", 2)))
        path = Path(os.getenv("REPORT_MEMORY_DB") or self.config.get("db_path") or "data/report_memory.sqlite")
        self.store = MemoryStore(path if path.is_absolute() else BASE_DIR / path, embedder,
                                 float(self.config.get("min_similarity", 0.45)))
        app.state.report_memory = self.store
        self.logger.info("started (%s questions, semantic=%s)", self.store.stats()["questions"], semantic)

    async def on_shutdown(self, app) -> None:
        self.store.close()
