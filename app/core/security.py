"""Request authentication shared by every module.

- Bigship token: the seller's panel token, forwarded to Bigship APIs on their behalf.
  Sent as `Authorization: Bearer <token>` (Postman, /docs) or, from the Angular panel via the
  AI_python_backend gateway, as `X-Shipment-Authorization: Bearer <token>`.
- Service token: when SERVICE_TOKEN is set, every /api request except health must carry it in
  `X-Ai-Box-Service-Token`. The gateway adds it, so only the gateway can reach this service.
"""
import secrets

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings

SERVICE_TOKEN_HEADER = "X-Ai-Box-Service-Token"

# Adds the "Authorize" button to /docs; Swagger then sends `Authorization: Bearer <token>`.
bigship_bearer = HTTPBearer(
    scheme_name="BigshipToken",
    description="Bigship panel token (localStorage `token`). Paste it without the `Bearer ` prefix.",
    auto_error=False,
)


def bigship_authorization(
    credentials: HTTPAuthorizationCredentials | None = Depends(bigship_bearer),
    x_shipment_authorization: str | None = Header(default=None, include_in_schema=False),
) -> str:
    """Return the `Authorization` header value to forward to Bigship, or 401."""
    token = credentials.credentials.strip() if credentials else ""
    if not token and x_shipment_authorization:
        scheme, _, value = x_shipment_authorization.strip().partition(" ")
        token = value.strip() if scheme.lower() == "bearer" else ""
    if not token:
        raise HTTPException(401, "Authorization: Bearer <Bigship token> header is required")
    return f"Bearer {token}"


async def service_token_middleware(request: Request, call_next):
    settings = get_settings()
    path = request.url.path
    if (settings.SERVICE_TOKEN and path.startswith(settings.API_PREFIX + "/")
            and path != f"{settings.API_PREFIX}/health"):
        sent = request.headers.get(SERVICE_TOKEN_HEADER, "")
        if not secrets.compare_digest(sent.encode(), settings.SERVICE_TOKEN.encode()):
            return JSONResponse({"detail": "Invalid or missing service token"}, status_code=403)
    return await call_next(request)
