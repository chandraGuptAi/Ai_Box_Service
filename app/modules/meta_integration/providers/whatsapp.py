"""WhatsApp Business Platform (Cloud API) provider.

Onboarding: Embedded Signup v4 returns an exchangeable code (30 s TTL) to the browser plus the
WABA / phone number IDs in a ``WA_EMBEDDED_SIGNUP`` message event. Those IDs are only *claims*:
the server exchanges the code, then requires the WABA to appear in the token's granular
``whatsapp_business_management`` targets and the phone number to belong to that WABA.

Reporting (aggregates only, no message content):
- ``analytics``         sent / delivered per day (WABA level)
- ``pricing_analytics`` message volume per pricing category per day
- ``template_analytics`` sent / delivered / read / clicked per template per day, only when the
  seller has enabled template insights in WhatsApp Manager (enabling is irreversible, so this
  integration never enables it on the seller's behalf)
- read / failed counts from message status webhooks (see webhooks.py)
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..errors import MetaError
from .base import (NOT_AVAILABLE, NOT_SYNCED, PERMISSION_REQUIRED, AccountGrant, AssetInfo, BaseMetaProvider,
                   FetchResult, MetricDef, MetricPoint, SyncWindow, TokenCheck, debug_token, number, parse_time, ts)
from .graph import GraphError

MGMT_SCOPE = "whatsapp_business_management"
TEMPLATE_WINDOW_DAYS = 89           # template analytics: 90-day lookback
TEMPLATE_BATCH = 10                 # template_ids per request (Meta max)


class WhatsAppProvider(BaseMetaProvider):
    platform = "whatsapp"
    required_scopes = (MGMT_SCOPE,)
    catalog = {d.name: d for d in (
        MetricDef("messages_sent", "messages", "Messages sent by the account's phone numbers", True),
        MetricDef("messages_delivered", "messages", "Messages delivered to recipients", True),
        MetricDef("pricing_volume", "messages", "Messages per pricing category (marketing, utility, ...)", True,
                  level="category"),
        MetricDef("template_sent", "messages", "Template messages sent, per template", True, level="template"),
        MetricDef("template_delivered", "messages", "Template messages delivered, per template", True,
                  level="template"),
        MetricDef("template_read", "messages", "Template messages read, per template (7-day read window)", True,
                  level="template"),
        MetricDef("template_clicked", "clicks", "Template button clicks, per template", True, level="template"),
        MetricDef("status_read", "messages", "Messages reported read by status webhooks since connection "
                  "(recipients can disable read receipts)", True),
        MetricDef("status_failed", "messages", "Messages reported failed by status webhooks since connection",
                  True),
    )}

    # ---- connection -----------------------------------------------------------------------

    async def exchange_code(self, code: str) -> str:
        payload = await self.graph.get("oauth/access_token", params={
            "client_id": self.settings.APP_ID, "client_secret": self.settings.APP_SECRET, "code": code})
        token = payload.get("access_token")
        if not token:
            raise MetaError("META_AUTHORIZATION_FAILED", platform=self.platform)
        return token

    async def validate_connection(self, token: str, account_id: str) -> TokenCheck:
        check = await debug_token(self.graph, token, self.settings.APP_ID, self.settings.APP_SECRET)
        if check.valid and account_id and MGMT_SCOPE in check.targets and account_id not in check.targets[MGMT_SCOPE]:
            check.valid = False   # the WABA was removed from the grant
        return check

    async def build_grant(self, token: str, waba_id: str, phone_number_id: str | None,
                          business_id: str | None, flow_event: str) -> AccountGrant:
        check = await self.validate_connection(token, "")
        if not check.valid:
            raise MetaError("META_AUTHORIZATION_FAILED", platform=self.platform)
        if MGMT_SCOPE not in check.scopes:
            raise MetaError("META_PERMISSION_DENIED", platform=self.platform,
                            details={"missing_scopes": [MGMT_SCOPE]})
        granted = check.targets.get(MGMT_SCOPE)
        # Never trust the browser-supplied WABA ID on its own.
        if not granted or waba_id not in granted:
            raise MetaError("META_PERMISSION_DENIED", "This WhatsApp Business Account was not granted to BigShip "
                            "during signup.", platform=self.platform)
        assets = await self.list_assets(token, waba_id, {"business_id": business_id, "flow_event": flow_event})
        phones = {a.external_id for a in assets if a.asset_type == "whatsapp_phone_number"}
        if phone_number_id and phone_number_id not in phones:
            raise MetaError("META_PERMISSION_DENIED", "The phone number does not belong to the authorized "
                            "WhatsApp Business Account.", platform=self.platform)
        waba = next(a for a in assets if a.asset_type == "whatsapp_business_account")
        return AccountGrant(external_account_id=waba_id, name=waba.name, access_token=token,
                            token_type="business_integration", expires_at=check.expires_at, scopes=check.scopes,
                            meta_user_id=check.meta_user_id, assets=assets)

    async def list_assets(self, token: str, account_id: str, metadata: dict[str, Any]) -> list[AssetInfo]:
        waba = await self.graph.get(account_id, token=token, params={"fields": "id,name,currency,timezone_id"})
        phones = await self.graph.paged(f"{account_id}/phone_numbers", token=token, params={
            "fields": "id,display_phone_number,verified_name,quality_rating,code_verification_status,"
                      "platform_type,status"})
        assets = [AssetInfo("whatsapp_business_account", str(waba.get("id") or account_id), waba.get("name"), {
            "currency": waba.get("currency"), "timezone_id": waba.get("timezone_id"),
            # Reported by the signup event; informational, not used for authorization.
            "business_portfolio_id": metadata.get("business_id"), "signup_flow": metadata.get("flow_event")})]
        for phone in phones:
            assets.append(AssetInfo("whatsapp_phone_number", str(phone["id"]),
                                    phone.get("verified_name") or phone.get("display_phone_number"), {
                                        "display_phone_number": phone.get("display_phone_number"),
                                        "verified_name": phone.get("verified_name"),
                                        "quality_rating": phone.get("quality_rating"),
                                        "code_verification_status": phone.get("code_verification_status"),
                                        "platform_type": phone.get("platform_type"),
                                        "status": phone.get("status")}))
        return assets

    async def subscribe_webhooks(self, token: str, waba_id: str) -> None:
        await self.graph.post(f"{waba_id}/subscribed_apps", token=token)

    async def register_phone(self, token: str, phone_number_id: str, pin: str) -> None:
        await self.graph.post(f"{phone_number_id}/register", token=token,
                              json_body={"messaging_product": "whatsapp", "pin": pin})

    async def disconnect(self, token: str, account_id: str) -> None:
        await self.graph.delete(f"{account_id}/subscribed_apps", token=token)

    # ---- insights ---------------------------------------------------------------------------

    async def fetch_insights(self, token: str, account_id: str, assets: list[AssetInfo],
                             window: SyncWindow) -> FetchResult:
        result = FetchResult()
        await self._messaging(token, account_id, window, result)
        await self._pricing(token, account_id, window, result)
        await self._templates(token, account_id, window, result)
        return result

    async def _messaging(self, token: str, waba: str, window: SyncWindow, result: FetchResult) -> None:
        field = f"analytics.start({ts(window.since)}).end({ts(window.until)}).granularity(DAY)"
        names = ["messages_sent", "messages_delivered"]
        try:
            payload = await self.graph.get(waba, token=token, params={"fields": field})
        except GraphError as exc:
            if self.fatal(exc):
                raise
            result.missing(names, self.reason_for(exc), "Messaging analytics are not available for this account.")
            return
        for point in (payload.get("analytics") or {}).get("data_points") or []:
            start, end = parse_time(point.get("start")), parse_time(point.get("end"))
            if not start or not end:
                continue
            for key, name in (("sent", "messages_sent"), ("delivered", "messages_delivered")):
                result.points.append(MetricPoint(name, number(point.get(key)), "day", start, end, waba, "messages"))
        result.ok(*names)

    async def _pricing(self, token: str, waba: str, window: SyncWindow, result: FetchResult) -> None:
        field = (f"pricing_analytics.start({ts(window.since)}).end({ts(window.until)}).granularity(DAILY)"
                 '.metric_types(["VOLUME"]).dimensions(["PRICING_CATEGORY"])')
        try:
            payload = await self.graph.get(waba, token=token, params={"fields": field})
        except GraphError as exc:
            if self.fatal(exc):
                raise
            result.missing(["pricing_volume"], self.reason_for(exc), "Pricing analytics are not available.")
            return
        for block in (payload.get("pricing_analytics") or {}).get("data") or []:
            for point in block.get("data_points") or []:
                start, end = parse_time(point.get("start")), parse_time(point.get("end"))
                if start and end:
                    result.points.append(MetricPoint(
                        "pricing_volume", number(point.get("volume")), "day", start, end, waba, "messages",
                        {"pricing_category": point.get("pricing_category") or "UNKNOWN"}))
        result.ok("pricing_volume")

    async def _templates(self, token: str, waba: str, window: SyncWindow, result: FetchResult) -> None:
        names = ["template_sent", "template_delivered", "template_read", "template_clicked"]
        since = max(window.since, window.until - timedelta(days=TEMPLATE_WINDOW_DAYS))
        try:
            templates = await self.graph.paged(f"{waba}/message_templates", token=token,
                                               params={"fields": "id,name,category,status", "limit": 100})
        except GraphError as exc:
            if self.fatal(exc):
                raise
            result.missing(names, self.reason_for(exc), "Message templates could not be listed.")
            return
        info = {str(t["id"]): t for t in templates if t.get("id")}
        if not info:
            result.missing(names, NOT_AVAILABLE, "The account has no message templates.")
            return
        ids = list(info)
        for i in range(0, len(ids), TEMPLATE_BATCH):
            batch = ids[i:i + TEMPLATE_BATCH]
            try:
                rows = await self.graph.paged(f"{waba}/template_analytics", token=token, params={
                    "start": ts(since), "end": ts(window.until), "granularity": "daily",
                    "metric_types": "sent,delivered,read,clicked", "template_ids": f"[{','.join(batch)}]"})
            except GraphError as exc:
                if self.fatal(exc):
                    raise
                status = PERMISSION_REQUIRED if exc.kind in {"permission", "invalid_param"} else NOT_SYNCED
                result.missing(names, status, "Template analytics are off for this account. The seller can turn "
                               "them on in WhatsApp Manager (Meta does not allow turning them off again).")
                return
            for block in rows:
                for point in block.get("data_points") or []:
                    start, end = parse_time(point.get("start")), parse_time(point.get("end"))
                    tid = str(point.get("template_id") or "")
                    if not (start and end and tid):
                        continue
                    t = info.get(tid, {})
                    dims = {"template_id": tid, "template_name": t.get("name"), "category": t.get("category")}
                    for key in ("sent", "delivered", "read"):
                        if key in point:
                            result.points.append(MetricPoint(f"template_{key}", number(point[key]), "day", start, end,
                                                             waba, "messages", dims))
                    clicks = point.get("clicked")
                    if isinstance(clicks, list):
                        total = sum(number(c.get("count")) or 0 for c in clicks if isinstance(c, dict))
                        result.points.append(MetricPoint("template_clicked", total, "day", start, end, waba,
                                                         "clicks", dims))
        result.ok(*names)

