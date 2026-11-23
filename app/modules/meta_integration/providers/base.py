"""Provider interface and the normalized types every provider returns."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .graph import GraphClient, GraphError

# Missing-data reasons (stored in meta_metric_status and returned to the report engine).
UNSUPPORTED = "unsupported"
PERMISSION_REQUIRED = "permission_required"
NOT_AVAILABLE = "not_available"
NOT_SYNCED = "not_synced"
EXPIRED_TOKEN = "expired_token"
AVAILABLE = "available"


@dataclass(frozen=True)
class MetricDef:
    name: str
    unit: str
    description: str
    # True only when summing values across days/assets gives a meaningful total.
    # Unique-people metrics (reach, unique views, accounts engaged) and snapshots are not additive.
    additive: bool
    period: str = "day"          # day | lifetime | snapshot
    level: str = "account"       # account | media | post | template | category


@dataclass
class AssetInfo:
    asset_type: str
    external_id: str
    name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AccountGrant:
    """One Meta account the seller authorized; becomes one meta_connections row."""
    external_account_id: str
    name: str | None
    access_token: str
    token_type: str
    expires_at: datetime | None
    scopes: list[str]
    meta_user_id: str | None
    assets: list[AssetInfo]


@dataclass
class MetricPoint:
    metric_name: str
    value: float | None
    period: str                      # day | lifetime | snapshot
    period_start: datetime
    period_end: datetime
    asset_external_id: str
    unit: str = "count"
    dimensions: dict[str, Any] = field(default_factory=dict)
    source: str = "graph_api"

    @property
    def dimension_key(self) -> str:
        return json.dumps(self.dimensions, sort_keys=True, separators=(",", ":")) if self.dimensions else ""


@dataclass
class MetricStatus:
    metric_name: str
    status: str
    reason: str | None = None


@dataclass
class FetchResult:
    points: list[MetricPoint] = field(default_factory=list)
    statuses: list[MetricStatus] = field(default_factory=list)

    def ok(self, *names: str) -> None:
        self.statuses.extend(MetricStatus(n, AVAILABLE) for n in names)

    def missing(self, names: list[str] | tuple[str, ...], status: str, reason: str) -> None:
        self.statuses.extend(MetricStatus(n, status, reason) for n in names)


@dataclass
class TokenCheck:
    valid: bool
    expires_at: datetime | None
    scopes: list[str]
    meta_user_id: str | None = None
    # scope -> target IDs (granular scopes), e.g. whatsapp_business_management -> [WABA IDs]
    targets: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class SyncWindow:
    since: datetime          # inclusive, UTC midnight
    until: datetime          # exclusive, UTC midnight

    @classmethod
    def last_days(cls, days: int, now: datetime | None = None) -> "SyncWindow":
        now = now or datetime.now(timezone.utc)
        until = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)
        return cls(until - timedelta(days=days), until)

    def days(self) -> list[tuple[datetime, datetime]]:
        out, day = [], self.since
        while day < self.until:
            out.append((day, day + timedelta(days=1)))
            day += timedelta(days=1)
        return out


def ts(value: datetime) -> int:
    return int(value.timestamp())


def parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(int(value), timezone.utc)
    text = str(value).replace("Z", "+00:00")
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":   # 2026-11-16T08:00:00+0000
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def debug_token(graph: GraphClient, token: str, app_id: str, app_secret: str) -> TokenCheck:
    """``GET /debug_token`` with the app token: validity, expiry, scopes and granular target IDs."""
    payload = await graph.get("debug_token", params={"input_token": token, "access_token": f"{app_id}|{app_secret}"})
    data = payload.get("data") or {}
    expires = data.get("expires_at")
    targets: dict[str, list[str]] = {}
    for item in data.get("granular_scopes") or []:
        if isinstance(item, dict) and item.get("scope"):
            targets[str(item["scope"])] = [str(t) for t in item.get("target_ids") or []]
    return TokenCheck(
        valid=bool(data.get("is_valid")) and str(data.get("app_id", app_id)) == str(app_id),
        expires_at=datetime.fromtimestamp(int(expires), timezone.utc) if expires else None,  # 0 = never
        scopes=[str(s) for s in data.get("scopes") or []],
        meta_user_id=str(data["user_id"]) if data.get("user_id") else None,
        targets=targets,
    )


class BaseMetaProvider(ABC):
    platform: str = ""
    #: Metrics this provider can produce, by name.
    catalog: dict[str, MetricDef] = {}
    #: Permissions the connection must hold for reporting.
    required_scopes: tuple[str, ...] = ()

    def __init__(self, graph: GraphClient, settings) -> None:
        self.graph = graph
        self.settings = settings

    @abstractmethod
    async def validate_connection(self, token: str, account_id: str) -> TokenCheck: ...

    @abstractmethod
    async def list_assets(self, token: str, account_id: str, metadata: dict[str, Any]) -> list[AssetInfo]: ...

    def fetch_available_metrics(self) -> list[MetricDef]:
        return list(self.catalog.values())

    @abstractmethod
    async def fetch_insights(self, token: str, account_id: str, assets: list[AssetInfo],
                             window: SyncWindow) -> FetchResult: ...

    async def disconnect(self, token: str, account_id: str) -> None:
        """Release platform-side resources (e.g. webhook subscriptions). Default: nothing to do."""

    async def refresh_token(self, token: str) -> tuple[str, datetime | None] | None:
        """Return a refreshed (token, expires_at) when the platform supports refresh."""
        return None

    def available_features(self) -> list[str]:
        return sorted({d.level for d in self.catalog.values()})

    # ---- helpers ---------------------------------------------------------------------------

    @staticmethod
    def reason_for(error: GraphError) -> str:
        return {"permission": PERMISSION_REQUIRED, "token_expired": EXPIRED_TOKEN, "token_invalid": EXPIRED_TOKEN,
                "invalid_param": UNSUPPORTED, "not_found": NOT_AVAILABLE}.get(error.kind, NOT_SYNCED)

    @staticmethod
    def fatal(error: GraphError) -> bool:
        """Errors that stop the whole sync (the job layer maps them to connection state/retries)."""
        return error.kind in {"token_expired", "token_invalid", "rate_limited", "network", "server"}
