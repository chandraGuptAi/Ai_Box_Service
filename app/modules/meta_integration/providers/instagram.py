"""Instagram provider. Two login modes, chosen by settings:

* Facebook Login for Business (``META_INSTAGRAM_CONFIG_ID`` set): the seller picks Instagram
  professional accounts (linked to a Facebook Page) in Meta's dialog on the main app. Calls go to
  graph.facebook.com with the linked Page's token (does not expire) or, without one, the long-lived
  user token. Scopes: ``instagram_basic`` + ``instagram_manage_insights``.
* Instagram Login (``META_INSTAGRAM_APP_ID``/``SECRET``): graph.instagram.com, works for professional
  accounts with or without a linked Facebook Page. Scopes: ``instagram_business_basic`` +
  ``instagram_business_manage_insights``. Newer apps may not get this product.

Personal accounts are rejected (``account_type`` must be BUSINESS or MEDIA_CREATOR). ``impressions``
was removed by Meta in 2025 and is not requested; ``views`` replaces it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from ..errors import MetaError
from .base import (NOT_AVAILABLE, AccountGrant, AssetInfo, BaseMetaProvider, FetchResult, MetricDef, MetricPoint,
                   SyncWindow, TokenCheck, debug_token, number, parse_time, ts)
from .graph import GraphError

SCOPES = ("instagram_business_basic", "instagram_business_manage_insights")
FB_SCOPES = ("instagram_basic", "instagram_manage_insights")
# Granular scopes whose target IDs are Facebook Page IDs.
FB_PAGE_SCOPES = ("pages_show_list", "pages_read_engagement", "business_management")
IG_FIELDS = "id,username,name,followers_count,media_count"
PAGE_FIELDS = f"id,name,access_token,instagram_business_account{{{IG_FIELDS}}}"
ELIGIBLE_TYPES = {"BUSINESS", "MEDIA_CREATOR"}
# Account metrics read as one total per day (metric_type=total_value).
DAILY_TOTALS = ("views", "accounts_engaged", "total_interactions", "likes", "comments", "shares", "saves",
                "replies", "profile_links_taps")
MEDIA_METRICS = ("views", "reach", "likes", "comments", "shares", "saved", "total_interactions")
MAX_MEDIA = 50


class InstagramProvider(BaseMetaProvider):
    platform = "instagram"
    required_scopes = SCOPES
    catalog = {d.name: d for d in (
        MetricDef("reach", "accounts", "Unique accounts that saw content, per day", False),
        MetricDef("views", "views", "Times content was played or displayed", True),
        MetricDef("accounts_engaged", "accounts", "Unique accounts that interacted, per day", False),
        MetricDef("total_interactions", "interactions", "Likes, saves, comments, shares and replies", True),
        MetricDef("likes", "likes", "Likes on posts, reels and videos", True),
        MetricDef("comments", "comments", "Comments on posts, reels and videos", True),
        MetricDef("shares", "shares", "Shares of posts, reels and videos", True),
        MetricDef("saves", "saves", "Saves of posts, reels and videos", True),
        MetricDef("replies", "replies", "Replies to stories", True),
        MetricDef("profile_links_taps", "taps", "Taps on profile contact links", True),
        MetricDef("follows", "accounts", "New follows per day", True),
        MetricDef("unfollows", "accounts", "Unfollows per day", True),
        MetricDef("followers_count", "accounts", "Followers at sync time", False, period="snapshot"),
        MetricDef("media_count", "media", "Published media at sync time", False, period="snapshot"),
        *(MetricDef(f"media_{m}", "count", f"Lifetime {m} of one post/reel", m not in {"reach"},
                    period="lifetime", level="media") for m in MEDIA_METRICS),
    )}

    @property
    def _app_id(self) -> str:
        return self.settings.INSTAGRAM_APP_ID

    @property
    def facebook_login(self) -> bool:
        return self.settings.instagram_uses_facebook_login

    @property
    def _host(self) -> str:
        return "facebook" if self.facebook_login else "instagram"

    def authorization_url(self, state: str, code_verifier: str | None = None) -> str:
        if self.facebook_login:
            return f"https://www.facebook.com/{self.graph.version}/dialog/oauth?" + urlencode({
                "client_id": self.settings.APP_ID, "redirect_uri": self.settings.OAUTH_REDIRECT_URI,
                "state": state, "response_type": "code", "config_id": self.settings.INSTAGRAM_CONFIG_ID,
                "override_default_response_type": "true"})
        return "https://www.instagram.com/oauth/authorize?" + urlencode({
            "client_id": self._app_id, "redirect_uri": self.settings.OAUTH_REDIRECT_URI, "response_type": "code",
            "scope": ",".join(SCOPES), "state": state, "enable_fb_login": "false"})

    async def exchange_code(self, code: str, code_verifier: str | None = None):
        """Instagram Login: code -> short-lived -> long-lived (60-day) token, returned as
        (token, expires_at, scopes, user_id). Facebook Login: the long-lived user token only."""
        if self.facebook_login:
            return await self._facebook_user_token(code)
        short = await self.graph.post("oauth/access_token", host="instagram_oauth", versioned=False, data={
            "client_id": self._app_id, "client_secret": self.settings.INSTAGRAM_APP_SECRET,
            "grant_type": "authorization_code", "redirect_uri": self.settings.OAUTH_REDIRECT_URI,
            "code": code.removesuffix("#_")})
        item = (short.get("data") or [short])[0] if isinstance(short.get("data"), list) else short
        token = item.get("access_token")
        if not token:
            raise MetaError("META_AUTHORIZATION_FAILED", platform=self.platform)
        perms = item.get("permissions") or []
        scopes = perms.split(",") if isinstance(perms, str) else [str(p) for p in perms]
        long = await self.graph.get("access_token", host="instagram", versioned=False, params={
            "grant_type": "ig_exchange_token", "client_secret": self.settings.INSTAGRAM_APP_SECRET,
            "access_token": token})
        expires = long.get("expires_in")
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires)) if expires else None
        return long["access_token"], expires_at, scopes, str(item.get("user_id") or "") or None

    async def build_grant(self, token: str, expires_at: datetime | None, scopes: list[str]) -> AccountGrant:
        missing = [s for s in SCOPES if scopes and s not in scopes]
        if missing:
            raise MetaError("META_PERMISSION_DENIED", platform=self.platform, details={"missing_scopes": missing})
        profile = await self._profile(token)
        if str(profile.get("account_type", "")).upper() not in ELIGIBLE_TYPES:
            raise MetaError("META_ACCOUNT_NOT_ELIGIBLE", "Only Instagram professional (business or creator) "
                            "accounts can be connected.", platform=self.platform)
        asset = self._asset(profile)
        return AccountGrant(external_account_id=asset.external_id, name=asset.name, access_token=token,
                            token_type="instagram_user", expires_at=expires_at, scopes=scopes or list(SCOPES),
                            meta_user_id=str(profile.get("id")), assets=[asset])

    # ---- Facebook Login for Business mode ------------------------------------------------------

    async def _facebook_user_token(self, code: str) -> str:
        s = self.settings
        payload = await self.graph.get("oauth/access_token", params={
            "client_id": s.APP_ID, "client_secret": s.APP_SECRET, "redirect_uri": s.OAUTH_REDIRECT_URI,
            "code": code.removesuffix("#_")})
        token = payload.get("access_token")
        if not token:
            raise MetaError("META_AUTHORIZATION_FAILED", platform=self.platform)
        try:   # Long-lived user token -> Page tokens that do not expire.
            long = await self.graph.get("oauth/access_token", params={
                "grant_type": "fb_exchange_token", "client_id": s.APP_ID, "client_secret": s.APP_SECRET,
                "fb_exchange_token": token})
            token = long.get("access_token") or token
        except GraphError as exc:
            if exc.kind not in {"invalid_param", "permission"}:
                raise
        return token

    async def build_grants(self, user_token: str) -> tuple[list[AccountGrant], list[dict[str, str]]]:
        """Facebook Login mode: one connection per Instagram professional account the seller shared."""
        s = self.settings
        check = await debug_token(self.graph, user_token, s.APP_ID, s.APP_SECRET)
        if not check.valid:
            raise MetaError("META_AUTHORIZATION_FAILED", platform=self.platform)
        missing = [x for x in FB_SCOPES if x not in check.scopes]
        if missing:
            raise MetaError("META_PERMISSION_DENIED", platform=self.platform,
                            details={"missing_scopes": missing, "granted_scopes": sorted(check.scopes)})
        found: dict[str, tuple[dict[str, Any], str, str, datetime | None]] = {}
        pages = await self._shared_pages(user_token, check.targets)
        for page in pages:
            ig = page.get("instagram_business_account")
            if not isinstance(ig, dict) or not ig.get("id"):
                continue
            # The linked Page's token never expires; fall back to the 60-day user token.
            token, kind, expires = ((page["access_token"], "page", None) if page.get("access_token")
                                    else (user_token, "user", check.expires_at))
            found.setdefault(str(ig["id"]), ({**ig, "page_id": str(page.get("id"))}, token, kind, expires))
        skipped: list[dict[str, str]] = []
        ig_targets = list(dict.fromkeys(i for scope in FB_SCOPES for i in check.targets.get(scope, [])))
        for ig_id in ig_targets:
            if ig_id in found:
                continue
            try:
                profile = await self.graph.get(ig_id, token=user_token, params={"fields": IG_FIELDS})
            except GraphError as exc:
                if exc.kind not in {"invalid_param", "permission", "not_found"}:
                    raise
                skipped.append({"external_account_id": ig_id, "reason": "instagram_account_not_readable"})
                continue
            found[ig_id] = (profile, user_token, "user", check.expires_at)
        grants = []
        for ig_id, (profile, token, kind, expires) in found.items():
            asset = self._fb_asset({**profile, "id": ig_id})
            grants.append(AccountGrant(external_account_id=ig_id, name=asset.name, access_token=token,
                                       token_type=kind, expires_at=expires, scopes=check.scopes,
                                       meta_user_id=check.meta_user_id, assets=[asset]))
        if not grants and not skipped:
            raise MetaError("META_PERMISSION_DENIED", "No Instagram professional account was shared with BigShip. "
                            "Select an Instagram account (linked to a Facebook Page) in the Meta dialog.",
                            platform=self.platform, details={
                                "granted_scopes": sorted(check.scopes),
                                # Counts only: enough to tell "no Page shared" from "Page has no Instagram".
                                "diagnostics": {"pages_found": len(pages),
                                                "pages_with_instagram": sum(1 for p in pages if isinstance(
                                                    p.get("instagram_business_account"), dict)),
                                                "granular_targets": {k: len(v) for k, v in check.targets.items()}}})
        return grants, skipped

    async def _shared_pages(self, user_token: str, targets: dict[str, list[str]]) -> list[dict[str, Any]]:
        pages = await self.graph.paged("me/accounts", token=user_token, params={"fields": PAGE_FIELDS, "limit": 100})
        # Business Login tokens can leave granted Pages out of me/accounts: read those directly.
        listed = {str(p.get("id")) for p in pages}
        page_ids = [p for p in dict.fromkeys(p for scope in FB_PAGE_SCOPES for p in targets.get(scope, []))
                    if p not in listed]
        for page_id in page_ids:
            try:
                pages.append(await self.graph.get(page_id, token=user_token, params={"fields": PAGE_FIELDS}))
            except GraphError as exc:
                if exc.kind not in {"invalid_param", "permission", "not_found"}:
                    raise
        return pages

    @staticmethod
    def _fb_asset(profile: dict[str, Any]) -> AssetInfo:
        return AssetInfo("instagram_account", str(profile["id"]), profile.get("username") or profile.get("name"), {
            "api_user_id": str(profile["id"]), "username": profile.get("username"),
            "page_id": profile.get("page_id"), "login": "facebook"})

    # ---- shared ---------------------------------------------------------------------------------

    async def _profile(self, token: str, account_id: str | None = None) -> dict[str, Any]:
        if self.facebook_login:
            return await self.graph.get(account_id or "me", token=token, params={"fields": IG_FIELDS})
        return await self.graph.get("me", host="instagram", token=token, params={
            "fields": "id,user_id,username,name,account_type,followers_count,media_count"})

    @staticmethod
    def _asset(profile: dict[str, Any]) -> AssetInfo:
        # user_id = professional account ID (used by webhooks); id = app-scoped ID used for API calls.
        external = str(profile.get("user_id") or profile["id"])
        return AssetInfo("instagram_account", external, profile.get("username") or profile.get("name"), {
            "api_user_id": str(profile["id"]), "username": profile.get("username"),
            "account_type": profile.get("account_type")})

    async def validate_connection(self, token: str, account_id: str) -> TokenCheck:
        try:
            profile = await self._profile(token, account_id)
        except GraphError as exc:
            if exc.kind in {"token_expired", "token_invalid"}:
                return TokenCheck(False, None, [])
            raise
        ok = str(profile.get("user_id") or profile.get("id")) == account_id
        return TokenCheck(ok, None, list(FB_SCOPES if self.facebook_login else SCOPES), str(profile.get("id")))

    async def list_assets(self, token: str, account_id: str, metadata: dict[str, Any]) -> list[AssetInfo]:
        profile = await self._profile(token, account_id)
        return [self._fb_asset(profile) if self.facebook_login else self._asset(profile)]

    async def refresh_token(self, token: str) -> tuple[str, datetime | None] | None:
        if self.facebook_login:   # Page tokens do not expire; a lapsed user token needs a reconnect.
            return None
        payload = await self.graph.get("refresh_access_token", host="instagram", versioned=False,
                                       params={"grant_type": "ig_refresh_token", "access_token": token})
        expires = payload.get("expires_in")
        return payload["access_token"], (datetime.now(timezone.utc) + timedelta(seconds=int(expires))
                                         if expires else None)

    # ---- insights ---------------------------------------------------------------------------

    async def fetch_insights(self, token: str, account_id: str, assets: list[AssetInfo],
                             window: SyncWindow) -> FetchResult:
        result = FetchResult()
        asset = assets[0]
        api_id = asset.metadata.get("api_user_id") or "me"
        profile = await self._profile(token, account_id)
        now = datetime.now(timezone.utc)
        for name in ("followers_count", "media_count"):
            if profile.get(name) is not None:
                result.points.append(MetricPoint(name, number(profile[name]), "snapshot", now, now, account_id,
                                                 self.catalog[name].unit))
        result.ok("followers_count", "media_count")
        await self._reach(token, api_id, account_id, window, result)
        await self._daily_totals(token, api_id, account_id, window, result)
        await self._follows(token, api_id, account_id, window, result)
        await self._media(token, api_id, account_id, window, result)
        return result

    async def _insights(self, token: str, api_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        payload = await self.graph.get(f"{api_id}/insights", host=self._host, token=token, params=params)
        return payload.get("data") or []

    async def _reach(self, token, api_id, account_id, window: SyncWindow, result: FetchResult) -> None:
        try:
            rows = await self._insights(token, api_id, {"metric": "reach", "period": "day",
                                                        "metric_type": "time_series", "since": ts(window.since),
                                                        "until": ts(window.until)})
        except GraphError as exc:
            if self.fatal(exc):
                raise
            result.missing(["reach"], self.reason_for(exc), "Reach is not available for this account.")
            return
        for row in rows:
            for value in row.get("values") or []:
                end = parse_time(value.get("end_time"))
                if end:
                    result.points.append(MetricPoint("reach", number(value.get("value")), "day",
                                                     end - timedelta(days=1), end, account_id, "accounts"))
        result.ok("reach")

    async def _daily_totals(self, token, api_id, account_id, window: SyncWindow, result: FetchResult) -> None:
        wanted, unavailable = list(DAILY_TOTALS), {}
        for start, end in window.days():
            if not wanted:
                break
            params = {"period": "day", "metric_type": "total_value", "since": ts(start), "until": ts(end)}
            try:
                rows = await self._insights(token, api_id, {**params, "metric": ",".join(wanted)})
            except GraphError as exc:
                if self.fatal(exc):
                    raise
                # One bad metric fails the batch: retry one by one and drop the unsupported ones.
                rows = []
                for metric in list(wanted):
                    try:
                        rows += await self._insights(token, api_id, {**params, "metric": metric})
                    except GraphError as single:
                        if self.fatal(single):
                            raise
                        unavailable[metric] = self.reason_for(single)
                        wanted.remove(metric)
            for row in rows:
                value = (row.get("total_value") or {}).get("value")
                if row.get("name") in self.catalog and value is not None:
                    result.points.append(MetricPoint(row["name"], number(value), "day", start, end, account_id,
                                                     self.catalog[row["name"]].unit))
        for metric, reason in unavailable.items():
            result.missing([metric], reason, "Meta did not return this metric for the account.")
        result.ok(*wanted)

    async def _follows(self, token, api_id, account_id, window: SyncWindow, result: FetchResult) -> None:
        names = ["follows", "unfollows"]
        for start, end in window.days():
            try:
                rows = await self._insights(token, api_id, {
                    "metric": "follows_and_unfollows", "period": "day", "metric_type": "total_value",
                    "breakdown": "follow_type", "since": ts(start), "until": ts(end)})
            except GraphError as exc:
                if self.fatal(exc):
                    raise
                result.missing(names, self.reason_for(exc), "Follow data is not available (accounts with fewer "
                               "than 100 followers get no follower insights).")
                return
            for row in rows:
                for breakdown in (row.get("total_value") or {}).get("breakdowns") or []:
                    for item in breakdown.get("results") or []:
                        kind = str((item.get("dimension_values") or [""])[0]).upper()
                        name = {"FOLLOWER": "follows", "NON_FOLLOWER": "unfollows"}.get(kind)
                        if name:
                            result.points.append(MetricPoint(name, number(item.get("value")), "day", start, end,
                                                             account_id, "accounts"))
        result.ok(*names)

    async def _media(self, token, api_id, account_id, window: SyncWindow, result: FetchResult) -> None:
        names = [f"media_{m}" for m in MEDIA_METRICS]
        try:
            media = await self.graph.paged(f"{api_id}/media", host=self._host, token=token, max_items=MAX_MEDIA,
                                           params={"fields": "id,media_type,media_product_type,timestamp",
                                                   "limit": 25})
        except GraphError as exc:
            if self.fatal(exc):
                raise
            result.missing(names, self.reason_for(exc), "Media could not be listed.")
            return
        # Newest first; keep media published inside the window.
        recent = [m for m in media if (parse_time(m.get("timestamp")) or window.until) >= window.since]
        if not recent:
            result.missing(names, NOT_AVAILABLE, "No media was published in the synced period.")
            return
        skipped = 0
        for item in recent:
            posted = parse_time(item.get("timestamp"))
            if posted is None:
                continue
            dims = {"media_id": str(item["id"]), "media_type": item.get("media_type"),
                    "media_product_type": item.get("media_product_type")}
            rows = None
            # Some media types reject some metrics, which fails the whole request: retry with the core set.
            for metrics in (MEDIA_METRICS, ("reach", "likes", "comments", "saved")):
                try:
                    rows = await self.graph.get(f"{item['id']}/insights", host=self._host, token=token,
                                                params={"metric": ",".join(metrics)})
                    break
                except GraphError as exc:
                    if self.fatal(exc):
                        raise
            if rows is None:
                skipped += 1
                continue
            for row in rows.get("data") or []:
                values = row.get("values") or [{}]
                value = values[0].get("value") if values else None
                if row.get("name") in MEDIA_METRICS:
                    result.points.append(MetricPoint(f"media_{row['name']}", number(value), "lifetime", posted,
                                                     datetime.now(timezone.utc), account_id, "count", dims))
        if skipped == len(recent):
            result.missing(names, NOT_AVAILABLE, "Meta returned no insights for the synced media.")
        else:
            result.ok(*names)
