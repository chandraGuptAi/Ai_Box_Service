"""Request and response bodies of /api/email."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Provider = Literal["gmail", "outlook"]


class ProviderInfo(BaseModel):
    provider: Provider
    name: str
    configured: bool


class Connection(BaseModel):
    connection_id: str
    provider: Provider
    account_email: str
    display_name: str | None = None
    status: Literal["connected", "reconnect_required", "disconnected"]
    connected_at: datetime | None = None
    last_used_at: datetime | None = None


class ProvidersResponse(BaseModel):
    enabled: bool = Field(description="False when the module is not configured on this server")
    providers: list[ProviderInfo]
    connections: list[Connection] = Field(default_factory=list,
                                          description="The caller's mailboxes (empty if the login is not verified)")


class ConnectionsResponse(BaseModel):
    connections: list[Connection]


class StartResponse(BaseModel):
    transaction_id: str
    provider: Provider
    authorization_url: str = Field(description="Open this in the browser tab")
    expires_at: datetime


class TransactionStatus(BaseModel):
    transaction_id: str
    provider: Provider
    status: Literal["pending", "authorized", "denied", "consumed", "expired"]
    expires_at: datetime


class CompleteRequest(BaseModel):
    transaction_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")


class NextUrlRequest(BaseModel):
    url: str = Field(max_length=2000, description="The Gmail / Outlook draft link")


class NextUrlResponse(BaseModel):
    url: str | None = None


class DisconnectResponse(BaseModel):
    connection_id: str
    status: Literal["disconnected"]


class DraftMetadata(BaseModel):
    """The JSON ``metadata`` part of POST /internal/draft (the PDF is the ``attachment`` part)."""
    connection_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    subject: str = Field(min_length=1, max_length=400)
    html: str = Field(min_length=1)
    text: str = ""
    report_id: str | None = Field(default=None, max_length=200)
    user_id: str | None = Field(default=None, max_length=200)


class DraftResponse(BaseModel):
    draft_id: str
    open_url: str = Field(description="Opens the draft in Gmail / Outlook on the web")
    provider: Provider
    from_email: str
    subject: str
    attachment_name: str | None = None
