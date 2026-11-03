"""HTTP client for the Voice AI admin API (`/v1/admin/call-logs`, `/v1/admin/call-analyses`).

The admin key (VOICEAI_ADMIN_KEY) is sent as `X-Admin-Key` from the server only; callers never see it.
"""
from typing import Any

import httpx
from fastapi import HTTPException


class VoiceAIAdminClient:
    def __init__(self, base_url: str, admin_key: str, timeout: float,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
                                         headers={"X-Admin-Key": admin_key})

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        res = await self._send("GET", path, params=params)
        try:
            body = res.json()
        except ValueError:
            raise HTTPException(502, "Voice AI returned invalid JSON")
        if not isinstance(body, dict):
            raise HTTPException(502, "Voice AI returned an unexpected response")
        return body

    async def open_recording(self, call_id: str, download: bool) -> httpx.Response:
        """A streaming response for one call's recording; the caller must close it."""
        request = self._client.build_request("GET", f"/v1/admin/call-logs/{call_id}/recording",
                                             params={"download": str(download).lower()})
        try:
            res = await self._client.send(request, stream=True)
        except httpx.TimeoutException:
            raise HTTPException(504, "Voice AI timed out")
        except httpx.RequestError:
            raise HTTPException(502, "Voice AI is unreachable")
        if res.status_code >= 400:
            await res.aread()
            await res.aclose()
            self._raise_for(res)
        return res

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            res = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise HTTPException(504, "Voice AI timed out")
        except httpx.RequestError:
            raise HTTPException(502, "Voice AI is unreachable")
        if res.status_code >= 400:
            self._raise_for(res)
        return res

    @staticmethod
    def _raise_for(res: httpx.Response) -> None:
        """Upstream failures: 404/422 keep their meaning; a rejected admin key is this server's fault (502)."""
        try:
            detail = res.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        if res.status_code in (401, 403):
            raise HTTPException(502, "Voice AI rejected the admin key (check VOICEAI_ADMIN_KEY)")
        if res.status_code in (404, 422):
            raise HTTPException(res.status_code, detail or "Voice AI could not find this call")
        raise HTTPException(502, f"Voice AI returned {res.status_code}")

    async def close(self) -> None:
        await self._client.aclose()
