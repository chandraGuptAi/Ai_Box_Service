"""Facebook Page provider (Facebook Login for Business, graph.facebook.com).

The seller picks Pages in Meta's dialog; ``/me/accounts`` then lists only Pages they granted. A
Page is connected only if the seller's role includes the ANALYZE task (required for insights);
each Page becomes its own connection with its own Page access token.

Metrics use the post-November-2025 names (``page_media_view`` etc.). Impression and fan
metrics were removed by Meta and are not requested. Insights requests are limited to 90 days.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from ..errors import MetaError
from .base import (NOT_AVAILABLE, AccountGrant, AssetInfo, BaseMetaProvider, FetchResult, MetricDef, MetricPoint,
                   SyncWindow, TokenCheck, debug_token, number, parse_time, ts)
from .graph import GraphError

SCOPES = ("pages_show_list", "pages_read_engagement", "read_insights")
PAGE_METRICS = ("page_media_view", "page_total_media_view_unique", "page_post_engagements", "page_views_total",
                "page_daily_follows_unique", "page_daily_unfollows_unique", "page_follows")
POST_METRICS = ("post_media_view", "post_total_media_view_unique", "post_clicks", "post_reactions_by_type_total")
MAX_POSTS = 50
MAX_RANGE_DAYS = 90


class FacebookProvider(BaseMetaProvider):
    platform = "facebook"
    required_scopes = SCOPES
    catalog = {d.name: d for d in (
        MetricDef("page_media_view", "views", "Times Page content was displayed or played", True),
        MetricDef("page_total_media_view_unique", "accounts", "Unique accounts that viewed Page content, per day",
                  False),
        MetricDef("page_post_engagements", "interactions", "Interactions with Page posts", True),
        MetricDef("page_views_total", "views", "Page profile views", True),
        MetricDef("page_daily_follows_unique", "accounts", "New Page follows per day (Meta estimate)", True),
        MetricDef("page_daily_unfollows_unique", "accounts", "Page unfollows per day (Meta estimate)", True),
        MetricDef("page_follows", "accounts", "Total Page followers at the end of the day", False),
        MetricDef("post_media_view", "views", "Lifetime views of one post", True, period="lifetime", level="post"),
        MetricDef("post_total_media_view_unique", "accounts", "Lifetime unique viewers of one post", False,
                  period="lifetime", level="post"),
        MetricDef("post_clicks", "clicks", "Lifetime clicks on one post", True, period="lifetime", level="post"),
        MetricDef("post_reactions", "reactions", "Lifetime reactions on one post, per reaction type", True,
                  period="lifetime", level="post"),
    )}

    # ---- connection -----------------------------------------------------------------------

    def authorization_url(self, state: str, code_verifier: str | None) -> str:
        params = {"client_id": self.settings.APP_ID, "redirect_uri": self.settings.OAUTH_REDIRECT_URI,
                  "state": state, "response_type": "code", "config_id": self.settings.FACEBOOK_CONFIG_ID,
                  "override_default_response_type": "true"}
        if code_verifier:
            digest = hashlib.sha256(code_verifier.encode()).digest()
            params |= {"code_challenge": base64.urlsafe_b64encode(digest).rstrip(b"=").decode(),
                       "code_challenge_method": "S256"}
        return f"https://www.facebook.com/{self.graph.version}/dialog/oauth?" + urlencode(params)

    async def exchange_code(self, code: str, code_verifier: str | None) -> str:
        params = {"client_id": self.settings.APP_ID, "client_secret": self.settings.APP_SECRET,
                  "redirect_uri": self.settings.OAUTH_REDIRECT_URI, "code": code}
        if code_verifier:
            params["code_verifier"] = code_verifier
        payload = await self.graph.get("oauth/access_token", params=params)
        token = payload.get("access_token")
        if not token:
            raise MetaError("META_AUTHORIZATION_FAILED", platform=self.platform)
        try:   # Long-lived user token -> Page tokens that do not expire.
            long = await self.graph.get("oauth/access_token", params={
                "grant_type": "fb_exchange_token", "client_id": self.settings.APP_ID,
                "client_secret": self.settings.APP_SECRET, "fb_exchange_token": token})
            token = long.get("access_token") or token
        except GraphError as exc:
            if exc.kind not in {"invalid_param", "permission"}:
                raise
        return token

    async def build_grants(self, user_token: str) -> tuple[list[AccountGrant], list[dict[str, str]]]:
        check = await debug_token(self.graph, user_token, self.settings.APP_ID, self.settings.APP_SECRET)
        if not check.valid:
            raise MetaError("META_AUTHORIZATION_FAILED", platform=self.platform)
        missing = [s for s in SCOPES if s not in check.scopes]
        if missing:
            raise MetaError("META_PERMISSION_DENIED", platform=self.platform,
                            details={"missing_scopes": missing, "granted_scopes": sorted(check.scopes)})
        fields = "id,name,category,tasks,access_token"
        pages = await self.graph.paged("me/accounts", token=user_token, params={"fields": fields, "limit": 100})
        if not pages:
            pages = await self._granted_pages(user_token, check.targets, fields)
        grants, skipped = [], []
        for page in pages:
            tasks = {str(t).upper() for t in page.get("tasks") or []}
            if not page.get("access_token"):
                skipped.append({"page_id": str(page.get("id")), "reason": "no_page_token"})
                continue
            if "ANALYZE" not in tasks:
                skipped.append({"page_id": str(page.get("id")), "name": page.get("name") or "",
                                "reason": "analyze_task_required"})
                continue
            asset = AssetInfo("facebook_page", str(page["id"]), page.get("name"),
                              {"category": page.get("category"), "tasks": sorted(tasks)})
            grants.append(AccountGrant(external_account_id=asset.external_id, name=asset.name,
                                       access_token=page["access_token"], token_type="page",
                                       expires_at=None, scopes=check.scopes, meta_user_id=check.meta_user_id,
                                       assets=[asset]))
        if not grants and not skipped:
            raise MetaError("META_PERMISSION_DENIED", "No Facebook Page was shared with BigShip. Select at least "
                            "one Page in the Meta dialog.", platform=self.platform)
        return grants, skipped

    async def _granted_pages(self, user_token: str, targets: dict[str, list[str]],
                             fields: str) -> list[dict[str, Any]]:
        """Pages granted through Facebook Login for Business.

        ``me/accounts`` can come back empty for a Business Login user token even though the seller picked
        Pages in the dialog; those Pages are listed in the token's granular scopes instead. Each one is read
        directly, and a Page that cannot be read is skipped later as ``no_page_token``.
        """
        page_ids: list[str] = []
        for scope in SCOPES:
            for page_id in targets.get(scope, []):
                if page_id not in page_ids:
                    page_ids.append(page_id)
        pages = []
        for page_id in page_ids:
            try:
                pages.append(await self.graph.get(page_id, token=user_token, params={"fields": fields}))
            except GraphError as exc:
                if exc.kind not in {"invalid_param", "permission", "not_found"}:
                    raise
                pages.append({"id": page_id})
        return pages

    async def validate_connection(self, token: str, account_id: str) -> TokenCheck:
        check = await debug_token(self.graph, token, self.settings.APP_ID, self.settings.APP_SECRET)
        if check.valid and check.targets.get("pages_read_engagement") and \
                account_id not in check.targets["pages_read_engagement"]:
            check.valid = False
        return check

    async def list_assets(self, token: str, account_id: str, metadata: dict[str, Any]) -> list[AssetInfo]:
        page = await self.graph.get(account_id, token=token, params={"fields": "id,name,category,followers_count"})
        return [AssetInfo("facebook_page", str(page["id"]), page.get("name"), {"category": page.get("category")})]

    # ---- insights ---------------------------------------------------------------------------

    async def fetch_insights(self, token: str, account_id: str, assets: list[AssetInfo],
                             window: SyncWindow) -> FetchResult:
        result = FetchResult()
        since = max(window.since, window.until - timedelta(days=MAX_RANGE_DAYS - 1))
        await self._page(token, account_id, since, window.until, result)
        await self._posts(token, account_id, since, window.until, result)
        return result

    async def _page_insights(self, token, page_id, metrics, since, until) -> list[dict[str, Any]]:
        payload = await self.graph.get(f"{page_id}/insights", token=token, params={
            "metric": ",".join(metrics), "period": "day", "since": ts(since), "until": ts(until)})
        return payload.get("data") or []

    async def _page(self, token, page_id, since, until, result: FetchResult) -> None:
        wanted = list(PAGE_METRICS)
        try:
            rows = await self._page_insights(token, page_id, wanted, since, until)
        except GraphError as exc:
            if self.fatal(exc):
                raise
            rows = []
            for metric in list(wanted):   # find which metric Meta rejected
                try:
                    rows += await self._page_insights(token, page_id, [metric], since, until)
                except GraphError as single:
                    if self.fatal(single):
                        raise
                    wanted.remove(metric)
                    result.missing([metric], self.reason_for(single), "Meta did not return this Page metric.")
        for row in rows:
            name = row.get("name")
            if name not in self.catalog:
                continue
            for value in row.get("values") or []:
                end = parse_time(value.get("end_time"))
                if end:
                    result.points.append(MetricPoint(name, number(value.get("value")), "day", end - timedelta(days=1),
                                                     end, page_id, self.catalog[name].unit))
        result.ok(*wanted)

    async def _posts(self, token, page_id, since, until, result: FetchResult) -> None:
        names = ["post_media_view", "post_total_media_view_unique", "post_clicks", "post_reactions"]
        try:
            posts = await self.graph.paged(f"{page_id}/posts", token=token, max_items=MAX_POSTS, params={
                "fields": "id,created_time,status_type", "since": ts(since), "until": ts(until), "limit": 25})
        except GraphError as exc:
            if self.fatal(exc):
                raise
            result.missing(names, self.reason_for(exc), "Page posts could not be listed.")
            return
        if not posts:
            result.missing(names, NOT_AVAILABLE, "No Page posts were published in the synced period.")
            return
        now = datetime.now(timezone.utc)
        rejected: set[str] = set()
        for post in posts:
            created = parse_time(post.get("created_time"))
            if created is None:
                continue
            metrics = [m for m in POST_METRICS if m not in rejected]
            try:
                rows = (await self.graph.get(f"{post['id']}/insights", token=token,
                                             params={"metric": ",".join(metrics)})).get("data") or []
            except GraphError as exc:
                if self.fatal(exc):
                    raise
                rows = []
                for metric in metrics:
                    try:
                        rows += (await self.graph.get(f"{post['id']}/insights", token=token,
                                                      params={"metric": metric})).get("data") or []
                    except GraphError as single:
                        if self.fatal(single):
                            raise
                        rejected.add(metric)
            dims = {"post_id": str(post["id"]), "status_type": post.get("status_type")}
            for row in rows:
                value = ((row.get("values") or [{}])[0] or {}).get("value")
                if row.get("name") == "post_reactions_by_type_total":
                    for reaction, count in (value or {}).items() if isinstance(value, dict) else []:
                        result.points.append(MetricPoint("post_reactions", number(count), "lifetime", created, now,
                                                         page_id, "reactions", {**dims, "reaction_type": reaction}))
                elif row.get("name") in self.catalog:
                    result.points.append(MetricPoint(row["name"], number(value), "lifetime", created, now, page_id,
                                                     self.catalog[row["name"]].unit, dims))
        for metric in rejected:
            result.missing(["post_reactions" if metric == "post_reactions_by_type_total" else metric],
                           "unsupported", "Meta rejected this post metric.")
        result.ok(*[n for n in names if not ({n, n + "_by_type_total"} & rejected)])
