"""Request/response models for /api/meta. No model here carries an access token or code back out."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Platform = Literal["whatsapp", "instagram", "facebook"]
DataSource = Literal["shipments", "whatsapp", "instagram", "facebook"]
_ID = r"^[0-9]{1,30}$"
_UUID = r"^[0-9a-fA-F-]{36}$"


class PlatformInfo(BaseModel):
    platform: Platform
    enabled: bool
    configured: bool
    flow: Literal["embedded_signup", "redirect"]
    available_features: list[str]
    missing_configuration: list[str] = Field(description="Setting names only; never values")
    seller_connected: bool | None = Field(None, description="Null when the caller's seller could not be verified")
    connection_status: Literal["not_connected", "pending", "connected", "expired", "reconnect_required",
                               "failed"] | None = Field(
        None, description="The seller's best connection state for this platform; null when unverified. "
                          "`configured` only means local settings are present, not that Meta approved the app.")


class PlatformsResponse(BaseModel):
    platforms: list[PlatformInfo]
    webhook_configured: bool
    seller_verified: bool = False


class ConnectionSummary(BaseModel):
    connection_id: str
    platform: Platform
    account_name: str | None
    external_account_id: str
    connection_status: Literal["pending", "connected", "expired", "reconnect_required", "disconnected", "failed"]
    reconnect_required: bool
    connected_at: datetime | None
    last_sync_at: datetime | None
    last_successful_sync_at: datetime | None
    data_stale: bool
    last_error_code: str | None
    available_features: list[str]
    active_sync_job_id: str | None = None


class ConnectionsResponse(BaseModel):
    connections: list[ConnectionSummary]


class AssetView(BaseModel):
    asset_id: str
    asset_type: str
    external_asset_id: str
    asset_name: str | None
    metadata: dict[str, Any]


class MetricAvailability(BaseModel):
    metric_name: str
    availability_status: str
    reason: str | None
    checked_at: datetime


class ConnectionDetail(ConnectionSummary):
    permission_scopes: list[str]
    assets: list[AssetView]
    metrics: list[MetricAvailability]


class EmbeddedSignupConfig(BaseModel):
    app_id: str
    graph_api_version: str
    config_id: str
    login_options: dict[str, Any] = Field(description="Pass as the 2nd argument of FB.login()")
    message_event_type: str
    allowed_origin_suffix: str
    code_ttl_seconds: int


class StartResponse(BaseModel):
    platform: Platform
    transaction_id: str
    expires_at: datetime
    flow: Literal["embedded_signup", "redirect"]
    authorization_url: str | None = Field(None, description="Redirect flows: open in the browser (same tab or popup)")
    embedded_signup: EmbeddedSignupConfig | None = None


class CompleteRequest(BaseModel):
    transaction_id: str = Field(pattern=_UUID)
    # WhatsApp Embedded Signup only:
    code: str | None = Field(None, min_length=10, max_length=2048,
                             description="authResponse.code from FB.login (WhatsApp); expires 30 s after issue")
    waba_id: str | None = Field(None, pattern=_ID, description="data.waba_id from the WA_EMBEDDED_SIGNUP event")
    phone_number_id: str | None = Field(None, pattern=_ID)
    business_id: str | None = Field(None, pattern=_ID)
    event: str | None = Field(None, max_length=60, description="FINISH, FINISH_ONLY_WABA, ...")
    pin: str | None = Field(None, pattern=r"^\d{6}$", description="Optional 6-digit two-step PIN to register the "
                            "number for Cloud API messaging. Used once, never stored.")

    @field_validator("event")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class CompletedConnection(ConnectionSummary):
    initial_sync_job_id: str
    notes: list[str]


class CompleteResponse(BaseModel):
    platform: Platform
    connections: list[CompletedConnection]
    skipped: list[dict[str, str]] = Field(description="Accounts not connected, with a reason code")


class TransactionStatus(BaseModel):
    transaction_id: str
    platform: Platform
    status: str
    expires_at: datetime


class DisconnectResponse(BaseModel):
    connection_id: str
    connection_status: Literal["disconnected"]
    data_deleted: bool
    token_deleted: bool


class SyncJob(BaseModel):
    job_id: str
    connection_id: str
    job_type: str
    status: Literal["queued", "running", "succeeded", "partial", "failed", "cancelled"]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    retry_count: int
    records_processed: int
    error_code: str | None
    next_attempt_at: datetime | None


class ReportContextRequest(BaseModel):
    data_sources: list[DataSource] = Field(min_length=1, max_length=4)
    from_date: date
    to_date: date
    timezone: str = Field("Asia/Kolkata", max_length=64)

    @model_validator(mode="after")
    def _range(self) -> "ReportContextRequest":
        if self.from_date > self.to_date:
            raise ValueError("from_date must be on or before to_date")
        if (self.to_date - self.from_date).days > 366:
            raise ValueError("the period can be at most 367 days")
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown timezone") from None
        return self


class ReportContextResponse(BaseModel):
    seller_scope: str
    requested_period: dict[str, str]
    generated_at: datetime
    sources: dict[str, dict[str, Any]]
    comparison_rules: list[str]
