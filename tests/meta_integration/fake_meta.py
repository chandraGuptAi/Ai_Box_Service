"""A programmable fake of the Meta Graph / Instagram APIs for httpx.MockTransport.

Routes are matched on (method, host, path regex) with the API version stripped from the path.
Tests override a route with ``fake.on(...)``; the newest matching route wins. Every request is
recorded (method, host, path, params) so tests can assert what was (not) sent.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs

import httpx

WABA = "1000000000000001"
PHONE = "2000000000000002"
OTHER_PHONE = "2000000000000099"
PAGE_OK = "3000000000000003"
PAGE_NO_ANALYZE = "3000000000000004"
IG_USER = "17841400000000005"
IG_APP_SCOPED = "9000000000000006"
META_USER = "4000000000000007"


def error(code: int, status: int = 400, subcode: int | None = None, headers: dict | None = None) -> httpx.Response:
    body = {"error": {"message": "secret-free test error", "type": "OAuthException", "code": code,
                      "fbtrace_id": "TRACE123"}}
    if subcode:
        body["error"]["error_subcode"] = subcode
    return httpx.Response(status, json=body, headers=headers)


@dataclass
class Call:
    method: str
    host: str
    path: str
    params: dict[str, str]
    headers: dict[str, str]
    body: bytes


@dataclass
class FakeMeta:
    calls: list[Call] = field(default_factory=list)
    routes: list[tuple[str, str, re.Pattern, Callable]] = field(default_factory=list)
    granted_wabas: list[str] = field(default_factory=lambda: [WABA])
    ig_account_type: str = "BUSINESS"

    def __post_init__(self) -> None:
        self._defaults()

    def on(self, method: str, host: str, pattern: str, handler: Callable | dict | httpx.Response) -> None:
        fn = handler if callable(handler) else (lambda req, m, h=handler: h if isinstance(h, httpx.Response)
                                                else httpx.Response(200, json=h))
        self.routes.insert(0, (method, host, re.compile(f"^{pattern}$"), fn))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def called(self, method: str, path_regex: str) -> list[Call]:
        rx = re.compile(path_regex)
        return [c for c in self.calls if c.method == method and rx.search(c.path)]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = re.sub(r"^/v\d+\.0", "", request.url.path)
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.calls.append(Call(request.method, request.url.host, path, params, dict(request.headers),
                               request.content))
        for method, host, rx, fn in self.routes:
            match = rx.match(path)
            if method == request.method and host == request.url.host and match:
                return fn(request, match)
        return httpx.Response(404, json={"error": {"message": "no fake route", "code": 803}})

    # ---- defaults: a healthy seller ---------------------------------------------------------

    def _defaults(self) -> None:
        fb, ig = "graph.facebook.com", "graph.instagram.com"

        def oauth(req, m):
            q = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
            if q.get("grant_type") == "fb_exchange_token":
                return httpx.Response(200, json={"access_token": "EAAlongLivedUserToken0000000000", "expires_in": 5e6})
            return httpx.Response(200, json={"access_token": "EAAbusinessToken000000000000000000",
                                             "token_type": "bearer"})
        self.on("GET", fb, r"/oauth/access_token", oauth)

        def debug(req, m):
            token = parse_qs(req.url.query.decode())["input_token"][0]
            if "UserToken" in token:   # Facebook Pages user token
                scopes = ["pages_show_list", "pages_read_engagement", "read_insights"]
                granular = [{"scope": "pages_read_engagement", "target_ids": [PAGE_OK, PAGE_NO_ANALYZE]}]
            else:
                scopes = ["whatsapp_business_management", "whatsapp_business_messaging"]
                granular = [{"scope": "whatsapp_business_management", "target_ids": self.granted_wabas}]
            return httpx.Response(200, json={"data": {"app_id": "111", "is_valid": True, "expires_at": 0,
                                                      "scopes": scopes, "granular_scopes": granular,
                                                      "user_id": META_USER}})
        self.on("GET", fb, r"/debug_token", debug)

        def waba(req, m):
            fields = parse_qs(req.url.query.decode()).get("fields", [""])[0]
            if fields.startswith("analytics"):
                return httpx.Response(200, json={"analytics": {"granularity": "DAY", "data_points": [
                    {"start": 1762905600, "end": 1762992000, "sent": 120, "delivered": 110},
                    {"start": 1762992000, "end": 1763078400, "sent": 80, "delivered": 75}]}, "id": WABA})
            if fields.startswith("pricing_analytics"):
                return httpx.Response(200, json={"pricing_analytics": {"data": [{"data_points": [
                    {"start": 1762905600, "end": 1762992000, "pricing_category": "MARKETING", "volume": 40},
                    {"start": 1762905600, "end": 1762992000, "pricing_category": "UTILITY", "volume": 80}]}]}})
            return httpx.Response(200, json={"id": WABA, "name": "Seller WABA", "currency": "INR",
                                             "timezone_id": "71"})
        self.on("GET", fb, rf"/{WABA}", waba)
        self.on("GET", fb, rf"/{WABA}/phone_numbers", {"data": [
            {"id": PHONE, "display_phone_number": "+91 98765 43210", "verified_name": "Seller Shop",
             "quality_rating": "GREEN", "platform_type": "CLOUD_API", "status": "CONNECTED"}]})
        self.on("POST", fb, rf"/{WABA}/subscribed_apps", {"success": True})
        self.on("DELETE", fb, rf"/{WABA}/subscribed_apps", {"success": True})
        self.on("POST", fb, rf"/{PHONE}/register", {"success": True})
        self.on("GET", fb, rf"/{WABA}/message_templates", {"data": [
            {"id": "555", "name": "order_update", "category": "UTILITY", "status": "APPROVED"}]})
        # Template insights not enabled for the WABA: Meta answers with a parameter error.
        self.on("GET", fb, rf"/{WABA}/template_analytics", lambda r, m: error(100))

        # ---- Facebook Pages ----
        self.on("GET", fb, r"/me/accounts", {"data": [
            {"id": PAGE_OK, "name": "Seller Page", "category": "Shopping", "access_token": "EAApageToken000000000",
             "tasks": ["ANALYZE", "ADVERTISE", "CREATE_CONTENT"]},
            {"id": PAGE_NO_ANALYZE, "name": "Other Page", "access_token": "EAApageToken2222222222",
             "tasks": ["MODERATE"]}]})
        self.on("GET", fb, rf"/{PAGE_OK}", {"id": PAGE_OK, "name": "Seller Page", "category": "Shopping"})

        def page_insights(req, m):
            metrics = parse_qs(req.url.query.decode())["metric"][0].split(",")
            if "page_views_total" in metrics and len(metrics) > 1:
                return error(100)   # one metric rejected -> provider retries one by one
            if metrics == ["page_views_total"]:
                return error(100)
            return httpx.Response(200, json={"data": [
                {"name": name, "period": "day", "values": [
                    {"value": 10 if name != "page_follows" else 500, "end_time": "2026-11-12T08:00:00+0000"},
                    {"value": 20 if name != "page_follows" else 505, "end_time": "2026-11-13T08:00:00+0000"}]}
                for name in metrics]})
        self.on("GET", fb, rf"/{PAGE_OK}/insights", page_insights)
        self.on("GET", fb, rf"/{PAGE_OK}/posts", {"data": [
            {"id": f"{PAGE_OK}_1", "created_time": "2026-11-12T10:00:00+0000", "status_type": "added_photos"}]})
        self.on("GET", fb, rf"/{PAGE_OK}_1/insights", {"data": [
            {"name": "post_media_view", "period": "lifetime", "values": [{"value": 900}]},
            {"name": "post_total_media_view_unique", "period": "lifetime", "values": [{"value": 700}]},
            {"name": "post_clicks", "period": "lifetime", "values": [{"value": 30}]},
            {"name": "post_reactions_by_type_total", "period": "lifetime", "values": [{"value": {"like": 12,
                                                                                                "love": 3}}]}]})

        # ---- Instagram (Instagram Login) ----
        self.on("POST", "api.instagram.com", r"/oauth/access_token", {"data": [{
            "access_token": "IGshortLived000000000000000000000000", "user_id": IG_USER,
            "permissions": "instagram_business_basic,instagram_business_manage_insights"}]})
        self.on("GET", ig, r"/access_token", {"access_token": "IGlongLived0000000000000000000000000",
                                              "token_type": "bearer", "expires_in": 5183944})
        self.on("GET", ig, r"/refresh_access_token", {"access_token": "IGrefreshed000000000000000000000000",
                                                      "token_type": "bearer", "expires_in": 5183944})
        self.on("GET", ig, r"/me", lambda r, m: httpx.Response(200, json={
            "id": IG_APP_SCOPED, "user_id": IG_USER, "username": "seller.shop", "account_type": self.ig_account_type,
            "followers_count": 1500, "media_count": 42}))

        def ig_insights(req, m):
            q = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
            metrics = q["metric"].split(",")
            if "replies" in metrics and len(metrics) > 1:
                return error(100)
            if metrics == ["replies"]:
                return error(100)
            if q.get("metric_type") == "time_series":
                return httpx.Response(200, json={"data": [{"name": "reach", "period": "day", "values": [
                    {"value": 300, "end_time": "2026-11-13T08:00:00+0000"}]}]})
            if metrics == ["follows_and_unfollows"]:
                return httpx.Response(200, json={"data": [{"name": "follows_and_unfollows", "total_value": {
                    "value": 7, "breakdowns": [{"dimension_keys": ["follow_type"], "results": [
                        {"dimension_values": ["FOLLOWER"], "value": 5},
                        {"dimension_values": ["NON_FOLLOWER"], "value": 2}]}]}}]})
            return httpx.Response(200, json={"data": [{"name": n, "period": "day", "total_value": {"value": 4}}
                                                      for n in metrics]})
        self.on("GET", ig, rf"/{IG_APP_SCOPED}/insights", ig_insights)
        self.on("GET", ig, rf"/{IG_APP_SCOPED}/media", {"data": []})


def body_json(call: Call) -> Any:
    return json.loads(call.body) if call.body else None
