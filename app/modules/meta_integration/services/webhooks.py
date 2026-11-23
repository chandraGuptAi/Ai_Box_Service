"""Webhook ingestion (fast, in the request) and processing (background worker).

Ingestion verifies ``X-Hub-Signature-256`` over the raw body, splits the payload into one event
per change, minimizes it (no message text, no customer phone numbers; message IDs are hashed)
and stores it with a content hash as ``event_id`` so Meta's retries are deduplicated.

Processing attributes an event to sellers only through stored, authorized assets: a WhatsApp
status counts for a connection only if both the WABA (entry ID) and the phone number ID in the
event belong to that connected account.
"""
from __future__ import annotations

import json
from typing import Any

from app.core.logger import get_logger

from ..context import MetaContext
from ..providers.base import parse_time
from ..security import sha256_hex, verify_webhook_signature

logger = get_logger("module.meta_integration.webhooks")

OBJECT_PLATFORM = {"whatsapp_business_account": "whatsapp", "instagram": "instagram", "page": "facebook"}
STATUS_VALUES = {"sent", "delivered", "read", "failed"}
# Non-personal keys kept from WhatsApp account/template/quality events.
_KEEP_KEYS = {"event", "message_template_id", "message_template_name", "message_template_language", "reason",
              "current_limit", "previous_quality_score", "new_quality_score", "decision", "requested_verified_name",
              "rejection_reason", "waba_info", "ban_info", "violation_info", "max_daily_conversation_per_phone",
              "max_phone_numbers_per_business", "max_phone_numbers_per_waba", "lock_status", "phone_number"}
DISCONNECTING_ACCOUNT_EVENTS = {"PARTNER_REMOVED", "ACCOUNT_DELETED", "DISABLED_UPDATE"}


class WebhookService:
    def __init__(self, ctx: MetaContext) -> None:
        self.ctx = ctx

    def verify_signature(self, raw: bytes, header: str | None) -> bool:
        s = self.ctx.settings
        return verify_webhook_signature(raw, header, [s.APP_SECRET, s.INSTAGRAM_APP_SECRET])

    async def ingest(self, raw: bytes) -> tuple[int, int]:
        """Store the events of a verified body. Returns (events, newly stored)."""
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return 0, 0
        events = extract_events(body)
        stored = await self.ctx.repo.insert_webhook_events(events) if events else 0
        return len(events), stored

    async def process_batch(self, limit: int = 50) -> int:
        events = await self.ctx.repo.claim_webhook_events(limit)
        for event in events:
            try:
                status = await self._process(event)
                await self.ctx.repo.mark_webhook_event(event["id"], status)
            except Exception as exc:   # noqa: BLE001
                logger.exception("Meta webhook event %s failed", event["id"])
                await self.ctx.repo.mark_webhook_event(event["id"], "failed", type(exc).__name__)
        return len(events)

    async def _process(self, event: dict[str, Any]) -> str:
        if event["platform"] != "whatsapp":
            return "ignored"   # no Instagram/Page webhook fields are subscribed by this integration
        payload = event["payload"]
        waba_id = event.get("external_account_id") or ""
        if event["event_type"] == "messages":
            phone_id = payload.get("phone_number_id") or ""
            targets = await self.ctx.repo.connections_for_phone_internal(waba_id, phone_id)
            if not targets:
                return "unmatched"
            rows = []
            for target in targets:
                for st in payload.get("statuses") or []:
                    at = parse_time(st.get("timestamp"))
                    if at and st.get("status") in STATUS_VALUES:
                        rows.append((target["connection_id"], target["seller_id"], phone_id, st["message_ref"],
                                     st["status"], at, st.get("pricing_category")))
            await self.ctx.repo.insert_status_events(rows)
            return "processed"
        if event["event_type"] == "account_update":
            targets = await self.ctx.repo.connections_for_asset_internal("whatsapp_business_account", waba_id)
            if not targets:
                return "unmatched"
            if str(payload.get("event", "")).upper() in DISCONNECTING_ACCOUNT_EVENTS:
                for t in targets:
                    await self.ctx.repo.set_status(t["connection_id"], "reconnect_required", "META_PERMISSION_DENIED")
                    await self.ctx.repo.audit("permissions_changed", seller_id=t["seller_id"],
                                              connection_id=t["connection_id"], platform="whatsapp", actor="meta",
                                              outcome="failure", details={"event": payload.get("event")})
            return "processed"
        return "ignored"


def extract_events(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    platform = OBJECT_PLATFORM.get(str(body.get("object")))
    if platform is None:
        return []
    events = []
    for entry in body.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        account = str(entry.get("id") or "")
        changes = entry.get("changes") or []
        if not changes and entry.get("messaging"):   # Messenger/Instagram messaging style
            changes = [{"field": "messaging", "value": {"count": len(entry["messaging"])}}]
        for change in changes:
            if not isinstance(change, dict):
                continue
            field = str(change.get("field") or "")
            value = change.get("value") if isinstance(change.get("value"), dict) else {}
            canonical = json.dumps({"a": account, "t": entry.get("time"), "f": field, "v": value},
                                   sort_keys=True, separators=(",", ":"), default=str)
            events.append({"platform": platform, "event_id": sha256_hex(f"{platform}|{canonical}"),
                           "external_account_id": account, "event_type": field,
                           "payload": minimize(platform, field, value)})
    return events


def minimize(platform: str, field: str, value: dict[str, Any]) -> dict[str, Any]:
    """Keep only what reporting needs. Message bodies and customer identifiers never get stored."""
    if platform == "whatsapp" and field == "messages":
        meta = value.get("metadata") or {}
        statuses = []
        for st in value.get("statuses") or []:
            if not isinstance(st, dict):
                continue
            statuses.append({"status": st.get("status"), "timestamp": st.get("timestamp"),
                             "message_ref": sha256_hex(str(st.get("id") or "")),
                             "pricing_category": (st.get("pricing") or {}).get("category"),
                             "error_codes": [e.get("code") for e in st.get("errors") or [] if isinstance(e, dict)]})
        messages = [m for m in value.get("messages") or [] if isinstance(m, dict)]
        types: dict[str, int] = {}
        for m in messages:
            types[str(m.get("type"))] = types.get(str(m.get("type")), 0) + 1
        return {"phone_number_id": str(meta.get("phone_number_id") or ""), "statuses": statuses,
                "inbound_messages": len(messages), "inbound_types": types}
    if platform == "whatsapp":
        return {k: v for k, v in value.items() if k in _KEEP_KEYS and not isinstance(v, (list,))}
    return {"field": field}
