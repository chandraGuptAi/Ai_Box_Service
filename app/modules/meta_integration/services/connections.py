"""Connection lifecycle: start, OAuth callback, complete, list, disconnect, manual sync, deauthorize.

Seller binding of a connection attempt:
- ``start`` creates a short-lived transaction owned by the authenticated seller.
- Redirect flows (Facebook, Instagram): Meta returns ``code`` + ``state`` to our callback. The
  callback only stores the code (encrypted) on the matching pending transaction and returns the
  browser to the Angular page with the transaction ID. No token exchange happens there.
- ``complete`` runs with the seller's authenticated request, consumes the transaction once, and
  only then exchanges the code. A transaction started by seller A can never be completed by B,
  which also defeats login-CSRF (an attacker's authorization link cannot land in a victim's
  seller account).
- WhatsApp Embedded Signup returns the code to the browser (JS SDK); ``complete`` receives it with
  the transaction ID and verifies the claimed WABA/phone IDs against the token's grants.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.core.logger import get_logger

from ..context import MetaContext
from ..errors import MetaError
from ..providers import PROVIDER_CLASSES
from ..providers.base import AccountGrant
from ..providers.graph import GraphError
from ..security import random_token, sha256_hex

logger = get_logger("module.meta_integration.connections")

WA_FINISH_EVENTS = {"FINISH", "FINISH_ONLY_WABA", "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING",
                    "FINISH_GRANT_ONLY_API_ACCESS"}


def graph_to_meta(exc: GraphError, platform: str, during_auth: bool = False) -> MetaError:
    if exc.kind == "token_expired":
        return MetaError("META_TOKEN_EXPIRED", platform=platform)
    if exc.kind == "token_invalid":
        return MetaError("META_AUTHORIZATION_FAILED" if during_auth else "META_RECONNECT_REQUIRED", platform=platform)
    if exc.kind == "permission":
        return MetaError("META_PERMISSION_DENIED", platform=platform)
    if exc.kind == "rate_limited":
        return MetaError("META_RATE_LIMITED", platform=platform, retry_after=exc.retry_after or 60)
    if during_auth and exc.kind == "invalid_param":
        return MetaError("META_AUTHORIZATION_FAILED", platform=platform)
    return MetaError("META_PROVIDER_ERROR", platform=platform)


class ConnectionService:
    def __init__(self, ctx: MetaContext) -> None:
        self.ctx = ctx

    # ---- read ---------------------------------------------------------------------------------

    def platforms(self) -> list[dict[str, Any]]:
        return describe_platforms(self.ctx.settings)

    def summary(self, row: dict[str, Any]) -> dict[str, Any]:
        status = row["connection_status"]
        last_ok = row.get("last_successful_sync_at")
        stale_before = datetime.now(timezone.utc) - timedelta(minutes=self.ctx.settings.STALE_AFTER_MINUTES)
        provider = self.ctx.providers.get(row["platform"])
        return {
            "connection_id": str(row["id"]), "platform": row["platform"],
            "account_name": row.get("external_account_name"), "external_account_id": row["external_account_id"],
            "connection_status": status,
            "reconnect_required": status in {"expired", "reconnect_required", "failed"},
            "connected_at": row.get("connected_at"), "last_sync_at": row.get("last_sync_at"),
            "last_successful_sync_at": last_ok,
            "data_stale": status != "connected" or last_ok is None or last_ok < stale_before,
            "last_error_code": row.get("last_error_code"),
            "available_features": provider.available_features() if provider else [],
            "active_sync_job_id": str(row["active_job_id"]) if row.get("active_job_id") else None,
        }

    async def list(self, seller_id: str) -> list[dict[str, Any]]:
        return [self.summary(r) for r in await self.ctx.repo.list_connections(seller_id)]

    async def detail(self, seller_id: str, connection_id: str) -> dict[str, Any]:
        row = await self._owned(seller_id, connection_id)
        assets = await self.ctx.repo.list_assets(row["id"])
        statuses = await self.ctx.repo.metric_statuses([row["id"]])
        return {**self.summary(row), "permission_scopes": row.get("permission_scopes") or [],
                "assets": [{"asset_id": str(a["id"]), "asset_type": a["asset_type"],
                            "external_asset_id": a["external_asset_id"], "asset_name": a["asset_name"],
                            "metadata": a["metadata"]} for a in assets],
                "metrics": [{"metric_name": s["metric_name"], "availability_status": s["availability_status"],
                             "reason": s["reason"], "checked_at": s["checked_at"]} for s in statuses]}

    async def _owned(self, seller_id: str, connection_id: str) -> dict[str, Any]:
        row = await self.ctx.repo.get_connection(seller_id, connection_id)
        if row is None:
            # Same answer for "absent" and "another seller's": no existence oracle across tenants.
            await self.ctx.repo.audit("access_denied", seller_id=seller_id, outcome="denied",
                                      details={"resource": "connection"})
            raise MetaError("META_CONNECTION_NOT_FOUND")
        return row

    # ---- start --------------------------------------------------------------------------------

    async def start(self, seller_id: str, platform: str) -> dict[str, Any]:
        provider = self.ctx.ensure_platform(platform)
        self.ctx.limiter.check(seller_id, "connect")
        s = self.ctx.settings
        state = random_token()
        state_hash = sha256_hex(state)
        verifier_blob = key_version = verifier = None
        if platform == "facebook" and s.FACEBOOK_PKCE:
            verifier = secrets.token_urlsafe(64)[:96]
            verifier_blob, key_version = self.ctx.encrypt(verifier, state_hash)
        tx = await self.ctx.repo.create_transaction(seller_id, platform, state_hash, s.OAUTH_TRANSACTION_TTL_SECONDS,
                                                    verifier_blob, key_version)
        await self.ctx.repo.audit("connection_started", seller_id=seller_id, platform=platform,
                                  details={"transaction_id": str(tx["id"])})
        base = {"platform": platform, "transaction_id": str(tx["id"]), "expires_at": tx["expires_at"]}
        if platform == "whatsapp":
            return {**base, "flow": "embedded_signup", "embedded_signup": {
                "app_id": s.APP_ID, "graph_api_version": s.GRAPH_API_VERSION, "config_id": s.WHATSAPP_CONFIG_ID,
                "login_options": {"config_id": s.WHATSAPP_CONFIG_ID, "response_type": "code",
                                  "override_default_response_type": True, "extras": {"setup": {}}},
                "message_event_type": "WA_EMBEDDED_SIGNUP", "allowed_origin_suffix": "facebook.com",
                "code_ttl_seconds": 30}}
        url = (provider.authorization_url(state, verifier) if platform == "facebook"
               else provider.authorization_url(state))
        return {**base, "flow": "redirect", "authorization_url": url}

    # ---- callback (browser redirect from Meta; no seller header) --------------------------------

    async def callback(self, state: str | None, code: str | None, error: str | None) -> str:
        status, tx = "invalid", None
        if state and len(state) <= 200:
            state_hash = sha256_hex(state)
            denied = bool(error) or not code
            blob = version = None
            if not denied:
                blob, version = self.ctx.encrypt(code.removesuffix("#_"), state_hash)
            tx = await self.ctx.repo.authorize_transaction(state_hash, blob, version, denied)
            if tx:
                status = "denied" if denied else "authorized"
                await self.ctx.repo.audit("connection_authorized" if not denied else "connection_failed",
                                          seller_id=tx["seller_id"], platform=tx["platform"], actor="meta",
                                          outcome="success" if not denied else "denied",
                                          details={"transaction_id": str(tx["id"]), "reason": (error or "")[:60]})
        query = {"meta_status": status}
        if tx:
            query |= {"meta_transaction": str(tx["id"]), "meta_platform": tx["platform"]}
        return _with_query(self.ctx.settings.FRONTEND_RETURN_URL, query)

    async def transaction_status(self, seller_id: str, transaction_id: str) -> dict[str, Any]:
        tx = await self.ctx.repo.transaction_status(seller_id, transaction_id)
        if tx is None:
            raise MetaError("META_TRANSACTION_INVALID")
        expired = tx["expires_at"] <= datetime.now(timezone.utc) and tx["status"] in {"pending", "authorized"}
        return {"transaction_id": str(tx["id"]), "platform": tx["platform"],
                "status": "expired" if expired else tx["status"], "expires_at": tx["expires_at"]}

    # ---- complete -------------------------------------------------------------------------------

    async def complete(self, seller_id: str, platform: str, body: dict[str, Any]) -> dict[str, Any]:
        provider = self.ctx.ensure_platform(platform)
        self.ctx.limiter.check(seller_id, "connect")
        from_status = "pending" if platform == "whatsapp" else "authorized"
        tx = await self.ctx.repo.consume_transaction(seller_id, body["transaction_id"], platform, from_status)
        if tx is None:
            await self.ctx.repo.audit("connection_failed", seller_id=seller_id, platform=platform, outcome="denied",
                                      details={"reason": "transaction_invalid"})
            raise MetaError("META_TRANSACTION_INVALID", platform=platform)
        try:
            if platform == "whatsapp":
                grants, skipped = [await self._whatsapp_grant(provider, body)], []
            elif platform == "instagram" and not self.ctx.settings.instagram_uses_facebook_login:
                code = self.ctx.cipher.decrypt(tx["encrypted_code"], tx["key_version"], tx["state_hash"])
                token, expires_at, scopes, _ = await provider.exchange_code(code)
                grants, skipped = [await provider.build_grant(token, expires_at, scopes)], []
            else:
                code = self.ctx.cipher.decrypt(tx["encrypted_code"], tx["key_version"], tx["state_hash"])
                verifier = (self.ctx.cipher.decrypt(tx["encrypted_verifier"], tx["key_version"], tx["state_hash"])
                            if tx.get("encrypted_verifier") else None)
                user_token = await provider.exchange_code(code, verifier)
                grants, skipped = await provider.build_grants(user_token)
        except GraphError as exc:
            await self._fail(seller_id, platform, exc.kind)
            raise graph_to_meta(exc, platform, during_auth=True) from None
        except MetaError as exc:
            await self._fail(seller_id, platform, exc.code, exc.details)
            raise

        connections = []
        for grant in grants:
            if not self.ctx.settings.ALLOW_SHARED_ACCOUNTS and await self.ctx.repo.other_sellers_on_account(
                    seller_id, platform, grant.external_account_id):
                skipped.append({"external_account_id": grant.external_account_id, "name": grant.name or "",
                                "reason": "connected_to_another_seller"})
                await self.ctx.repo.audit("connection_failed", seller_id=seller_id, platform=platform,
                                          outcome="denied", details={"reason": "connected_to_another_seller",
                                                                     "external_account_id": grant.external_account_id})
                continue
            connections.append(await self._save(seller_id, platform, grant, body))
        if not connections:
            if any(s.get("reason") == "connected_to_another_seller" for s in skipped):
                raise MetaError("META_ACCOUNT_CONNECTED_ELSEWHERE", platform=platform)
            raise MetaError("META_PERMISSION_DENIED", "None of the shared accounts can be used for reports.",
                            platform=platform, details={"skipped": skipped})
        return {"platform": platform, "connections": connections, "skipped": skipped}

    async def _whatsapp_grant(self, provider, body: dict[str, Any]) -> AccountGrant:
        event = str(body.get("event") or "FINISH").upper()
        if event not in WA_FINISH_EVENTS:
            raise MetaError("META_AUTHORIZATION_FAILED", "WhatsApp signup was not finished.", platform="whatsapp")
        token = await provider.exchange_code(body["code"])
        return await provider.build_grant(token, body["waba_id"], body.get("phone_number_id"),
                                          body.get("business_id"), event)

    async def _save(self, seller_id: str, platform: str, grant: AccountGrant, body: dict[str, Any]) -> dict:
        row, created = await self.ctx.repo.save_grant(seller_id, platform, grant, self.ctx.encrypt)
        notes: list[str] = []
        if platform == "whatsapp":
            notes += await self._whatsapp_post_signup(seller_id, row, grant, body)
        job, _ = await self.ctx.repo.enqueue_job(seller_id, row["id"], "initial")
        await self.ctx.repo.audit("connection_completed", seller_id=seller_id, connection_id=row["id"],
                                  platform=platform, details={"created": created, "scopes": grant.scopes,
                                                              "assets": len(grant.assets), "notes": notes})
        logger.info("Meta %s connection %s %s for seller", platform, row["id"], "created" if created else "renewed")
        return {**self.summary({**row, "active_job_id": job["id"]}), "initial_sync_job_id": str(job["id"]),
                "notes": notes}

    async def _whatsapp_post_signup(self, seller_id, row, grant: AccountGrant, body: dict[str, Any]) -> list[str]:
        provider = self.ctx.providers["whatsapp"]
        notes = []
        try:   # Required for message status webhooks (read/failed counts).
            await provider.subscribe_webhooks(grant.access_token, grant.external_account_id)
        except GraphError as exc:
            notes.append("webhook_subscription_failed")
            await self.ctx.repo.audit("webhook_subscription_failed", seller_id=seller_id, connection_id=row["id"],
                                      platform="whatsapp", outcome="failure", actor="system",
                                      details={"kind": exc.kind, "code": exc.code})
        event = str(body.get("event") or "FINISH").upper()
        pin, phone_id = body.get("pin"), body.get("phone_number_id")
        if event == "FINISH" and phone_id:
            if pin:
                try:   # The PIN is used once here and never stored or logged.
                    await provider.register_phone(grant.access_token, phone_id, pin)
                    notes.append("phone_number_registered")
                except GraphError as exc:
                    notes.append("phone_registration_failed")
                    await self.ctx.repo.audit("phone_registration_failed", seller_id=seller_id,
                                              connection_id=row["id"], platform="whatsapp", outcome="failure",
                                              details={"kind": exc.kind, "code": exc.code})
            else:
                notes.append("phone_registration_pending")   # reporting works; sending messages needs registration
        elif event == "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING":
            notes.append("coexistence_number_already_registered")
        return notes

    async def _fail(self, seller_id: str, platform: str, reason: str,
                    extra: dict[str, Any] | None = None) -> None:
        # Keep only non-secret diagnostics (e.g. missing_scopes) next to the reason.
        details: dict[str, Any] = {"reason": reason}
        for key in ("missing_scopes", "granted_scopes", "diagnostics"):
            if extra and key in extra:
                details[key] = extra[key] if isinstance(extra[key], dict) else list(extra[key])
        await self.ctx.repo.audit("connection_failed", seller_id=seller_id, platform=platform, outcome="failure",
                                  details=details)

    # ---- disconnect / sync ----------------------------------------------------------------------

    async def disconnect(self, seller_id: str, connection_id: str, delete_data: bool) -> dict[str, Any]:
        row = await self._owned(seller_id, connection_id)
        released = False
        if row["connection_status"] != "disconnected":
            try:
                token = await self.ctx.token_for(row["id"], row["platform"])
                await self.ctx.provider(row["platform"]).disconnect(token, row["external_account_id"])
                released = True
            except (GraphError, MetaError, KeyError, ValueError) as exc:
                logger.info("Meta disconnect cleanup skipped for %s: %s", row["id"], type(exc).__name__)
        await self.ctx.repo.disconnect(seller_id, row["id"], delete_data)
        await self.ctx.repo.audit("account_disconnected", seller_id=seller_id, connection_id=row["id"],
                                  platform=row["platform"], details={"delete_data": delete_data,
                                                                     "platform_cleanup": released})
        return {"connection_id": str(row["id"]), "connection_status": "disconnected", "data_deleted": delete_data,
                "token_deleted": True}

    async def request_sync(self, seller_id: str, connection_id: str) -> tuple[dict[str, Any], bool]:
        row = await self._owned(seller_id, connection_id)
        self.ctx.limiter.check(seller_id, "sync")
        if row["connection_status"] != "connected":
            raise MetaError("META_RECONNECT_REQUIRED", platform=row["platform"])
        last = await self.ctx.repo.last_manual_job(row["id"])
        wait = self.ctx.settings.MANUAL_SYNC_MIN_INTERVAL_SECONDS
        if last and (datetime.now(timezone.utc) - last).total_seconds() < wait:
            remaining = int(wait - (datetime.now(timezone.utc) - last).total_seconds())
            raise MetaError("META_SELLER_RATE_LIMITED", "A manual sync was requested recently.",
                            platform=row["platform"], retry_after=max(1, remaining))
        job, created = await self.ctx.repo.enqueue_job(seller_id, row["id"], "manual")
        if created:
            await self.ctx.repo.audit("sync_requested", seller_id=seller_id, connection_id=row["id"],
                                      platform=row["platform"], details={"job_id": str(job["id"])})
        return job, created

    async def job(self, seller_id: str, job_id: str) -> dict[str, Any]:
        job = await self.ctx.repo.get_job(seller_id, job_id)
        if job is None:
            raise MetaError("META_JOB_NOT_FOUND")
        return job

    # ---- Meta platform callbacks (signed_request) -------------------------------------------------

    async def deauthorize_meta_user(self, meta_user_id: str, delete_data: bool, actor_action: str) -> int:
        rows = await self.ctx.repo.connections_for_meta_user_internal([meta_user_id])
        for row in rows:
            await self.ctx.repo.deauthorize_internal(row["id"], delete_data)
            await self.ctx.repo.audit(actor_action, seller_id=row["seller_id"], connection_id=row["id"],
                                      platform=row["platform"], actor="meta", details={"delete_data": delete_data})
        return len(rows)


def describe_platforms(settings) -> list[dict[str, Any]]:
    """Readiness per platform from settings alone (works even when the database is down)."""
    out = []
    for name, cls in PROVIDER_CLASSES.items():
        problems = settings.platform_problems(name)
        out.append({"platform": name, "enabled": name in settings.ENABLED_PLATFORMS,
                    "configured": not problems, "flow": "embedded_signup" if name == "whatsapp" else "redirect",
                    "available_features": sorted({d.level for d in cls.catalog.values()}),
                    "missing_configuration": problems})
    return out


def _with_query(url: str, extra: dict[str, str]) -> str:
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query)) | extra
    return urlunparse(parts._replace(query=urlencode(query)))


def job_view(job: dict[str, Any]) -> dict[str, Any]:
    return {"job_id": str(job["id"]), "connection_id": str(job["connection_id"]), "job_type": job["job_type"],
            "status": job["status"], "created_at": job["created_at"], "started_at": job.get("started_at"),
            "completed_at": job.get("completed_at"), "retry_count": job.get("retry_count", 0),
            "records_processed": job.get("records_processed", 0), "error_code": job.get("last_error"),
            "next_attempt_at": job.get("run_after") if job["status"] == "queued" else None}
