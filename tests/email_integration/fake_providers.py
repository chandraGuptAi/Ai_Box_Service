"""In-memory Google + Microsoft OAuth and mail APIs, served through an httpx.MockTransport."""
from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx

GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
UPLOAD_URL = "https://upload.test/session-1"


class FakeProviders:
    def __init__(self) -> None:
        self.gmail_drafts: list[bytes] = []
        self.outlook_drafts: list[dict] = []
        self.outlook_attachments: list[dict] = []
        self.upload_chunks: list[httpx.Request] = []
        self.deleted: list[str] = []
        self.revoked: list[str] = []
        self.token_requests: list[dict] = []
        self.revoked_refresh_tokens: set[str] = set()
        # Behaviour of the next mail API calls: an HTTP status to fail with, or "timeout".
        self.next_api: list[int | str] = []
        self.ms_refresh_counter = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        host, path, method = request.url.host, request.url.path, request.method
        if host == "oauth2.googleapis.com" and path == "/token":
            return self._google_token(parse_qs(request.content.decode()))
        if host == "oauth2.googleapis.com" and path == "/revoke":
            self.revoked.append(parse_qs(request.content.decode())["token"][0])
            return httpx.Response(200)
        if host == "openidconnect.googleapis.com":
            return httpx.Response(200, json={"email": "Seller@Gmail.com", "email_verified": True, "name": "Indu"})
        if host == "gmail.googleapis.com" and path == "/upload/gmail/v1/users/me/drafts":
            failure = self._failure(request)
            if failure:
                return failure
            self.gmail_drafts.append(request.content)
            n = len(self.gmail_drafts)
            return httpx.Response(200, json={"id": f"r-{n}", "message": {"id": f"18c0abc{n}", "threadId": "t"}})
        if host == "login.microsoftonline.com" and path.endswith("/token"):
            return self._ms_token(parse_qs(request.content.decode()))
        if host == "graph.microsoft.com":
            return self._graph(request, path, method)
        if host == "upload.test":
            assert "authorization" not in request.headers, "the upload URL must not get the bearer token"
            self.upload_chunks.append(request)
            return httpx.Response(200)
        return httpx.Response(404, json={"error": "unexpected test URL"})

    def _failure(self, request: httpx.Request) -> httpx.Response | None:
        behaviour = self.next_api.pop(0) if self.next_api else None
        if behaviour == "timeout":
            raise httpx.ReadTimeout("no answer", request=request)
        if isinstance(behaviour, int):
            return httpx.Response(behaviour, json={"error": {"code": "TestError", "message": "secret details"}},
                                  headers={"Retry-After": "30"} if behaviour == 429 else None)
        return None

    def _graph(self, request: httpx.Request, path: str, method: str) -> httpx.Response:
        if path == "/v1.0/me":
            return httpx.Response(200, json={"displayName": "Ops", "mail": None, "userPrincipalName": "Ops@Contoso.com"})
        if path == "/v1.0/me/messages" and method == "POST":
            failure = self._failure(request)
            if failure:
                return failure
            self.outlook_drafts.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "AAMk1",
                                             "webLink": "https://outlook.office365.com/owa/?ItemID=AAMk1"})
        if path == "/v1.0/me/messages/AAMk1/attachments":
            failure = self._failure(request)
            if failure:
                return failure
            self.outlook_attachments.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "att-1"})
        if path == "/v1.0/me/messages/AAMk1/attachments/createUploadSession":
            self.outlook_attachments.append(json.loads(request.content))
            return httpx.Response(201, json={"uploadUrl": UPLOAD_URL})
        if path == "/v1.0/me/messages/AAMk1" and method == "DELETE":
            self.deleted.append("AAMk1")
            return httpx.Response(204)
        return httpx.Response(404, json={"error": "unexpected graph URL"})

    def _google_token(self, form: dict) -> httpx.Response:
        self.token_requests.append(form)
        if form["grant_type"][0] == "authorization_code":
            if not form.get("code_verifier"):
                return httpx.Response(400, json={"error": "invalid_request"})
            scope = "openid email" if form["code"][0] == "no-compose" else f"openid email {GMAIL_COMPOSE_SCOPE}"
            return httpx.Response(200, json={"access_token": "g-access-1", "refresh_token": "g-refresh",
                                             "expires_in": 3600, "scope": scope})
        if form["refresh_token"][0] in self.revoked_refresh_tokens:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"access_token": "g-access-2", "expires_in": 3600,
                                         "scope": f"openid email {GMAIL_COMPOSE_SCOPE}"})

    def _ms_token(self, form: dict) -> httpx.Response:
        self.token_requests.append(form)
        self.ms_refresh_counter += 1
        if form.get("refresh_token", [""])[0] in self.revoked_refresh_tokens:
            return httpx.Response(400, json={"error": "invalid_grant"})
        # Microsoft rotates the refresh token on every call.
        return httpx.Response(200, json={
            "access_token": f"ms-access-{self.ms_refresh_counter}",
            "refresh_token": f"ms-refresh-{self.ms_refresh_counter}", "expires_in": 3600,
            "scope": "https://graph.microsoft.com/Mail.ReadWrite https://graph.microsoft.com/User.Read openid email"})
