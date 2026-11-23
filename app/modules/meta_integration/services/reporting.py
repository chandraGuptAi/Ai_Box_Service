"""Report data source: synchronized, normalized Meta metrics for the AI Box report engine.

This never calls Meta. It reads what the sync stored and states plainly what is missing, how
fresh the data is and which date range is really covered, so the report engine can ground its
narrative and never present stale or absent data as current.

Rules carried into the response (``comparison_rules``) for the report engine and AI:
- every metric belongs to exactly one source/account; totals are given only for additive metrics;
- values from different sources are never summed;
- no shipment <-> Meta conversation/post linkage (no shared identifiers exist);
- correlation is not causation.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from ..context import MetaContext
from ..errors import MetaError
from ..providers.base import EXPIRED_TOKEN, NOT_SYNCED
from .connections import ConnectionService

META_SOURCES = ("whatsapp", "instagram", "facebook")
TOP_ITEMS = 20

PLATFORM_CAVEATS = {
    "whatsapp": [
        "Counts are aggregates from Meta; message content is not collected.",
        "Read and failed counts come from status webhooks received since the account was connected; recipients "
        "can disable read receipts, so read counts are a lower bound.",
        "Template read and click data is only recorded by Meta for 7 days after sending.",
    ],
    "instagram": [
        "Meta may delay Instagram insights by up to 48 hours.",
        "Reach and accounts engaged count unique accounts per day; they cannot be added up across days.",
        "Accounts with fewer than 100 followers do not get follower insights.",
    ],
    "facebook": [
        "Daily Page insights follow Meta's day boundaries (Pacific Time), not IST.",
        "Unique viewer counts cannot be added up across days.",
        "Impression and Page-like metrics were retired by Meta in 2025 and are not available.",
    ],
}
COMPARISON_RULES = [
    "Report each metric under its own source and account; never add values from different sources together.",
    "Totals are provided only for additive metrics; for others use daily values or the latest value.",
    "There is no identifier linking shipments to WhatsApp conversations or social posts; do not link them.",
    "Describe co-movement between sources as correlation only, never as cause and effect.",
    "When coverage is partial or data is stale, say so and give the covered dates.",
]


def _f(value: Any) -> float | None:
    if value is None:
        return None
    return float(value) if isinstance(value, (Decimal, int, float)) else None


class ReportingService:
    def __init__(self, ctx: MetaContext, connections: ConnectionService) -> None:
        self.ctx = ctx
        self.connections = connections

    async def context(self, seller_id: str, data_sources: list[str], from_date: date, to_date: date,
                      tz_name: str = "Asia/Kolkata") -> dict[str, Any]:
        tz = ZoneInfo(tz_name)
        start = datetime.combine(from_date, time.min, tz).astimezone(timezone.utc)
        end = datetime.combine(to_date + timedelta(days=1), time.min, tz).astimezone(timezone.utc)
        all_conns = await self.ctx.repo.list_connections(seller_id)
        sources: dict[str, Any] = {}
        for source in dict.fromkeys(data_sources):
            if source == "shipments":
                sources[source] = {"status": "external", "note": "Shipment data comes from the existing shipment "
                                   "report pipeline (POST /api/shipments/fetch)."}
                continue
            conns = [c for c in all_conns if c["platform"] == source]
            sources[source] = await self._source(seller_id, source, conns, start, end, from_date, to_date)
        return {"seller_scope": "authenticated_seller", "requested_period": {
                    "from_date": from_date.isoformat(), "to_date": to_date.isoformat(), "timezone": tz_name},
                "generated_at": datetime.now(timezone.utc), "sources": sources,
                "comparison_rules": COMPARISON_RULES}

    async def _source(self, seller_id, platform, conns, start, end, from_date, to_date) -> dict[str, Any]:
        if not conns:
            return {"status": "not_connected", "error": {
                "code": "META_NOT_CONNECTED", "action": "connect", "platform": platform,
                "message": MetaError("META_NOT_CONNECTED", platform=platform).message}, "accounts": []}
        provider = self.ctx.provider(platform)
        ids = [c["id"] for c in conns]
        rows = await self.ctx.repo.report_rows(seller_id, ids, start, end)
        statuses = await self.ctx.repo.metric_statuses(ids)
        wa_counts = (await self.ctx.repo.whatsapp_status_counts(seller_id, ids, start, end)
                     if platform == "whatsapp" else [])
        by_conn: dict[Any, list] = defaultdict(list)
        for r in rows:
            by_conn[r["connection_id"]].append(r)
        for r in wa_counts:   # webhook-derived daily counts, shaped like insight rows
            by_conn[r["connection_id"]].append({
                "metric_name": f"status_{r['status']}", "metric_value": r["n"], "aggregation_period": "day",
                "period_start": r["day"].replace(tzinfo=timezone.utc), "dimensions": {}, "source": "webhook"})
        status_by_conn: dict[Any, dict[str, dict]] = defaultdict(dict)
        for s in statuses:
            status_by_conn[s["connection_id"]][s["metric_name"]] = s

        accounts = []
        for conn in conns:
            summary = self.connections.summary(conn)
            accounts.append(self._account(provider, summary, conn, by_conn[conn["id"]], status_by_conn[conn["id"]],
                                          from_date, to_date))
        usable = [a for a in accounts if a["metrics"]]
        status = "ok" if usable and all(not a["data_stale"] for a in accounts) else "stale" if usable else "no_data"
        if all(a["reconnect_required"] for a in accounts):
            status = "reconnect_required"
        return {"status": status, "accounts": accounts, "caveats": PLATFORM_CAVEATS[platform]}

    def _account(self, provider, summary, conn, rows, statuses, from_date: date, to_date: date) -> dict[str, Any]:
        catalog = provider.catalog
        grouped: dict[str, list] = defaultdict(list)
        for r in rows:
            grouped[r["metric_name"]].append(r)
        metrics, items = {}, {}
        covered: set[date] = set()
        for name, mrows in grouped.items():
            definition = catalog.get(name)
            if definition is None:
                continue
            base = {"unit": definition.unit, "description": definition.description, "additive": definition.additive,
                    "source": mrows[0].get("source", "graph_api")}
            if definition.level in {"media", "post"}:
                items[name] = {**base, "items": self._items(mrows)}
                continue
            if definition.period == "snapshot":
                latest = max(mrows, key=lambda r: r["period_start"])
                metrics[name] = {**base, "kind": "snapshot", "value": _f(latest["metric_value"]),
                                 "as_of": latest["period_start"]}
                continue
            daily: dict[str, dict[str, float]] = defaultdict(dict)   # dimension -> day -> value
            for r in mrows:
                day = r["period_start"].astimezone(timezone.utc).date()
                covered.add(day)
                label = self._dimension_label(r.get("dimensions") or {})
                value = _f(r["metric_value"])
                if value is not None:
                    daily[label][day.isoformat()] = daily[label].get(day.isoformat(), 0.0) + value \
                        if definition.additive else value
            entry: dict[str, Any] = {**base, "kind": "daily"}
            if list(daily) == [""]:
                series = daily[""]
                entry["daily"] = [{"date": d, "value": v} for d, v in sorted(series.items())]
                entry["days_with_data"] = len(series)
                if definition.additive:
                    entry["total"] = sum(series.values())
                else:
                    entry["latest"] = series[max(series)] if series else None
            else:
                entry["by_dimension"] = [
                    {"dimension": label, "total" if definition.additive else "latest":
                     sum(s.values()) if definition.additive else (s[max(s)] if s else None),
                     "days_with_data": len(s)} for label, s in sorted(daily.items())]
            metrics[name] = entry

        unavailable = []
        for name in catalog:
            if name in metrics or name in items:
                continue
            st = statuses.get(name)
            if summary["reconnect_required"]:
                reason, detail = EXPIRED_TOKEN, "The account must be reconnected."
            elif st and st["availability_status"] != "available":
                reason, detail = st["availability_status"], st["reason"]
            elif st:
                reason, detail = "not_available", "Meta returned no data for the requested period."
            else:
                reason, detail = NOT_SYNCED, "Not synced yet."
            unavailable.append({"metric": name, "reason": reason, "detail": detail})

        days = (to_date - from_date).days + 1
        coverage = {"first_date": min(covered).isoformat() if covered else None,
                    "last_date": max(covered).isoformat() if covered else None,
                    "days_with_data": len(covered), "requested_days": days,
                    "complete": bool(covered) and min(covered) <= from_date and max(covered) >= to_date}
        return {"connection_id": summary["connection_id"], "account_name": summary["account_name"],
                "external_account_id": summary["external_account_id"],
                "connection_status": summary["connection_status"],
                "reconnect_required": summary["reconnect_required"],
                "last_successful_sync_at": summary["last_successful_sync_at"], "data_stale": summary["data_stale"],
                "coverage": coverage, "metrics": metrics, "items": items, "unavailable_metrics": unavailable}

    @staticmethod
    def _dimension_label(dims: dict[str, Any]) -> str:
        for key in ("pricing_category", "template_name", "template_id", "reaction_type"):
            if dims.get(key):
                return str(dims[key])
        return ""

    @staticmethod
    def _items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Lifetime per-post/media values (one row per item), largest first."""
        merged: dict[str, dict[str, Any]] = {}
        for r in rows:
            dims = r.get("dimensions") or {}
            key = str(dims.get("media_id") or dims.get("post_id") or "")
            item = merged.setdefault(key, {"id": key, "published_at": r["period_start"],
                                           "type": dims.get("media_product_type") or dims.get("status_type"),
                                           "value": 0.0})
            item["value"] += _f(r["metric_value"]) or 0.0
            if dims.get("reaction_type"):
                item.setdefault("by_reaction", {})[dims["reaction_type"]] = _f(r["metric_value"])
        return sorted(merged.values(), key=lambda i: i["value"], reverse=True)[:TOP_ITEMS]
