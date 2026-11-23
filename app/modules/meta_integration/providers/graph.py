"""Shared HTTP client for graph.facebook.com and graph.instagram.com.

- Access tokens go in the Authorization header (Facebook host) or the ``access_token`` query
  parameter (Instagram host, as Meta documents), and never into logs: only method, host, path,
  status, Meta error code/subcode and ``fbtrace_id`` are logged.
- ``appsecret_proof`` is added to every Facebook-host call made with a token.
- Meta errors are mapped to :class:`GraphError` with a machine-readable ``kind``.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.logger import get_logger

from ..security import appsecret_proof

logger = get_logger("module.meta_integration.graph")

_SECRET_PARAMS = re.compile(r"((?:access_token|input_token|fb_exchange_token|appsecret_proof|client_secret|code|"
                            r"code_verifier)=)[^&\s\"']+", re.I)


class _RedactQuerySecrets(logging.Filter):
    """httpx logs every request URL at INFO; strip token-bearing query parameters from it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(_SECRET_PARAMS.sub(r"\1[REDACTED]", str(a)) if isinstance(a, (str, httpx.URL))
                                else a for a in (record.args if isinstance(record.args, tuple) else (record.args,)))
        if isinstance(record.msg, str):
            record.msg = _SECRET_PARAMS.sub(r"\1[REDACTED]", record.msg)
        return True


_redactor = _RedactQuerySecrets()
for _name in ("httpx", "httpcore"):
    if _redactor not in logging.getLogger(_name).filters:
        logging.getLogger(_name).addFilter(_redactor)

# Meta error codes (Graph API "Handling errors" + WhatsApp Cloud API error codes).
_RATE_LIMIT_CODES = {4, 17, 32, 613, 80001, 80002, 80003, 80004, 80005, 80006, 80008, 80009, 80014,
                     130429, 131048, 131056}
_PERMISSION_CODES = {3, 10, 200, 294, 299}
_TOKEN_CODES = {102, 190}
_EXPIRED_SUBCODES = {463, 467}


@dataclass
class GraphError(Exception):
    kind: str            # token_expired | token_invalid | permission | rate_limited | invalid_param | not_found | server | network
    status: int
    code: int | None = None
    subcode: int | None = None
    trace_id: str | None = None
    retry_after: int | None = None
    # Meta's own message, kept for server-side diagnostics only; never sent to sellers.
    meta_message: str = ""

    def __str__(self) -> str:
        return f"GraphError({self.kind}, status={self.status}, code={self.code}, subcode={self.subcode})"


def classify(status: int, payload: Any, headers: httpx.Headers | None = None) -> GraphError:
    err = payload.get("error") if isinstance(payload, dict) else None
    err = err if isinstance(err, dict) else {}
    code = err.get("code") if isinstance(err.get("code"), int) else None
    subcode = err.get("error_subcode") if isinstance(err.get("error_subcode"), int) else None
    trace = err.get("fbtrace_id")
    message = str(err.get("message", ""))[:300]
    retry_after = None
    if headers is not None and headers.get("retry-after", "").isdigit():
        retry_after = int(headers["retry-after"])
    if status == 429 or code in _RATE_LIMIT_CODES:
        kind = "rate_limited"
        retry_after = retry_after or _usage_retry_after(headers)
    elif code in _TOKEN_CODES:
        kind = "token_expired" if subcode in _EXPIRED_SUBCODES else "token_invalid"
    elif code in _PERMISSION_CODES or 200 <= (code or 0) <= 299 or status == 403:
        kind = "permission"
    elif code == 100 or status == 400:
        kind = "invalid_param"
    elif status == 404:
        kind = "not_found"
    else:
        kind = "server"
    return GraphError(kind, status, code, subcode, trace, retry_after, message)


def _usage_retry_after(headers: httpx.Headers | None) -> int | None:
    """``X-Business-Use-Case-Usage`` / ``X-App-Usage`` carry ``estimated_time_to_regain_access`` (minutes)."""
    if headers is None:
        return None
    raw = headers.get("x-business-use-case-usage")
    try:
        usage = json.loads(raw) if raw else {}
        minutes = max((int(item.get("estimated_time_to_regain_access", 0))
                       for items in usage.values() for item in items), default=0)
        return minutes * 60 or None
    except (ValueError, TypeError, AttributeError):
        return None


class GraphClient:
    def __init__(self, version: str, app_secret: str, *, instagram_app_secret: str = "",
                 timeout: float = 20.0, transport: httpx.AsyncBaseTransport | None = None,
                 facebook_host: str = "https://graph.facebook.com",
                 instagram_host: str = "https://graph.instagram.com",
                 instagram_oauth_host: str = "https://api.instagram.com") -> None:
        self.version = version
        self._app_secret = app_secret
        self._ig_secret = instagram_app_secret
        self._hosts = {"facebook": facebook_host.rstrip("/"), "instagram": instagram_host.rstrip("/"),
                       "instagram_oauth": instagram_oauth_host.rstrip("/")}
        self._http = httpx.AsyncClient(transport=transport, follow_redirects=False,
                                       timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
                                       headers={"User-Agent": "bigship-ai-box-meta/1.0"})

    async def close(self) -> None:
        await self._http.aclose()

    async def request(self, method: str, path: str, *, token: str | None = None, host: str = "facebook",
                      params: dict[str, Any] | None = None, data: dict[str, Any] | None = None,
                      json_body: Any = None, versioned: bool = True) -> Any:
        base = self._hosts[host]
        url = f"{base}/{self.version}/{path.lstrip('/')}" if versioned else f"{base}/{path.lstrip('/')}"
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {}
        if token:
            if host == "facebook":
                headers["Authorization"] = f"Bearer {token}"
                if self._app_secret:
                    params["appsecret_proof"] = appsecret_proof(token, self._app_secret)
            else:
                params["access_token"] = token
        try:
            resp = await self._http.request(method, url, params=params, data=data, json=json_body, headers=headers)
        except httpx.TimeoutException as exc:
            logger.warning("Meta %s %s%s timed out", method, host, _safe_path(path))
            raise GraphError("network", 504, meta_message="timeout") from exc
        except httpx.HTTPError as exc:
            logger.warning("Meta %s %s%s failed: %s", method, host, _safe_path(path), type(exc).__name__)
            raise GraphError("network", 502, meta_message=type(exc).__name__) from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if resp.status_code >= 400 or (isinstance(payload, dict) and "error" in payload):
            error = classify(resp.status_code, payload, resp.headers)
            logger.warning("Meta %s %s%s -> %s kind=%s code=%s subcode=%s trace=%s", method, host,
                           _safe_path(path), resp.status_code, error.kind, error.code, error.subcode, error.trace_id)
            raise error
        return payload

    async def get(self, path: str, **kw) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw) -> Any:
        return await self.request("POST", path, **kw)

    async def delete(self, path: str, **kw) -> Any:
        return await self.request("DELETE", path, **kw)

    async def paged(self, path: str, *, token: str, host: str = "facebook", params: dict[str, Any] | None = None,
                    max_items: int = 500) -> list[dict[str, Any]]:
        """Follow ``paging.cursors.after`` until exhausted or ``max_items`` collected."""
        items: list[dict[str, Any]] = []
        params = dict(params or {})
        while len(items) < max_items:
            page = await self.get(path, token=token, host=host, params=params)
            items.extend(page.get("data") or [])
            after = ((page.get("paging") or {}).get("cursors") or {}).get("after")
            if not after or not (page.get("paging") or {}).get("next"):
                break
            params["after"] = after
        return items[:max_items]


def _safe_path(path: str) -> str:
    # Paths hold only object IDs and edge names; strip any query a caller may have embedded.
    return "/" + path.split("?", 1)[0].lstrip("/")
