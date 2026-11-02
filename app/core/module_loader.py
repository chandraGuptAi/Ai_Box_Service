"""Discovers, loads and registers modules listed in ``modules.yaml``."""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI

from app.core.config import get_settings
from app.core.logger import get_logger
from app.core.module_base import BaseModule

logger = get_logger("module_loader")

MODULES_PACKAGE = "app.modules"


class ModuleRegistry:
    """Holds loaded module instances so modules can find each other."""

    def __init__(self) -> None:
        self._modules: dict[str, BaseModule] = {}

    def add(self, module: BaseModule) -> None:
        self._modules[module.name] = module

    def get(self, name: str) -> BaseModule | None:
        return self._modules.get(name)

    def all(self) -> list[BaseModule]:
        return list(self._modules.values())

    def __contains__(self, name: str) -> bool:
        return name in self._modules


def read_modules_config(path: Path) -> dict[str, dict[str, Any]]:
    """Return ``{module_name: config}`` for every enabled module in the YAML file."""
    if not path.exists():
        logger.warning("Modules file %s not found, no modules loaded", path)
        return {}

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    enabled: dict[str, dict[str, Any]] = {}
    for name, cfg in (data.get("modules") or {}).items():
        cfg = cfg or {}
        if cfg.get("enabled", True):
            enabled[name] = cfg
    return enabled


def load_module(name: str, config: dict[str, Any]) -> BaseModule:
    py_module = importlib.import_module(f"{MODULES_PACKAGE}.{name}.module")
    module_cls = getattr(py_module, "Module", None)
    if module_cls is None or not issubclass(module_cls, BaseModule):
        raise TypeError(f"Module '{name}' must define `class Module(BaseModule)` in module.py")
    instance = module_cls(config)
    if instance.name != name:
        raise ValueError(f"Module folder '{name}' does not match Module.name '{instance.name}'")
    return instance


def register_modules(app: FastAPI) -> ModuleRegistry:
    settings = get_settings()
    registry = ModuleRegistry()

    for name, cfg in read_modules_config(settings.MODULES_FILE).items():
        registry.add(load_module(name, cfg))

    for module in registry.all():
        missing = [dep for dep in module.depends_on if dep not in registry]
        if missing:
            raise RuntimeError(f"Module '{module.name}' requires disabled/missing modules: {missing}")

        router = module.get_router()
        if router is not None:
            app.include_router(
                router,
                prefix=settings.API_PREFIX + module.prefix,
                tags=[module.title or module.name],
                include_in_schema=module.show_in_docs,
            )
        logger.info("Loaded module: %s", module.name)

    app.state.modules = registry
    return registry
