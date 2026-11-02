"""Base class every module must extend.

A module is a self-contained folder in ``app/modules/<name>/`` that contains a
``module.py`` exposing a class named ``Module`` (subclass of ``BaseModule``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from app.core.logger import get_logger

if TYPE_CHECKING:
    from fastapi import FastAPI


class BaseModule:
    #: Unique module name (must match the folder name).
    name: str = ""
    #: Section name shown in the API docs (defaults to `name`).
    title: str = ""
    #: Human readable description.
    description: str = ""
    #: Other modules this one needs to be enabled.
    depends_on: list[str] = []

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = config or {}
        self.logger = get_logger(f"module.{self.name}")

    @property
    def prefix(self) -> str:
        """URL prefix for this module's routes (override via ``prefix`` in modules.yaml)."""
        return self.config.get("prefix", f"/{self.name}")

    @property
    def show_in_docs(self) -> bool:
        """Whether the module's routes appear in /docs (set ``show_in_docs`` in modules.yaml)."""
        return bool(self.config.get("show_in_docs", True))

    def get_router(self) -> APIRouter | None:
        """Return the module's router, or None if it exposes no HTTP routes."""
        return None

    async def on_startup(self, app: FastAPI) -> None:
        """Called once when the app starts (open connections, load models, ...)."""

    async def on_shutdown(self, app: FastAPI) -> None:
        """Called once when the app stops (close connections, free resources, ...)."""
