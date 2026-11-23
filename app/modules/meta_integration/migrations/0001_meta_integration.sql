-- Meta integration schema. Applied once by the module's migration runner (see db.py).
-- gen_random_uuid() is built in from PostgreSQL 13.

CREATE TABLE meta_connections (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id               text NOT NULL,
    platform                text NOT NULL CHECK (platform IN ('whatsapp', 'instagram', 'facebook')),
    external_account_id     text NOT NULL,
    external_account_name   text,
    -- App-scoped Meta user ID that authorized the connection (deauthorize / data deletion callbacks).
    meta_user_id            text,
    connection_status       text NOT NULL DEFAULT 'pending' CHECK (connection_status IN
                            ('pending', 'connected', 'expired', 'reconnect_required', 'disconnected', 'failed')),
    permission_scopes       text[] NOT NULL DEFAULT '{}',
    connected_at            timestamptz,
    disconnected_at         timestamptz,
    last_sync_at            timestamptz,
    last_successful_sync_at timestamptz,
    last_error_code         text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    -- One row per seller and Meta account; reconnecting reuses it.
    CONSTRAINT meta_connections_seller_account UNIQUE (seller_id, platform, external_account_id)
);
CREATE INDEX meta_connections_seller_idx ON meta_connections (seller_id);
CREATE INDEX meta_connections_account_idx ON meta_connections (platform, external_account_id);
CREATE INDEX meta_connections_status_idx ON meta_connections (connection_status);
CREATE INDEX meta_connections_meta_user_idx ON meta_connections (meta_user_id) WHERE meta_user_id IS NOT NULL;

CREATE TABLE meta_assets (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id         text NOT NULL,
    connection_id     uuid NOT NULL REFERENCES meta_connections (id) ON DELETE CASCADE,
    asset_type        text NOT NULL CHECK (asset_type IN
                      ('whatsapp_business_account', 'whatsapp_phone_number', 'instagram_account', 'facebook_page')),
    external_asset_id text NOT NULL,
    asset_name        text,
    metadata          jsonb NOT NULL DEFAULT '{}',
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT meta_assets_unique UNIQUE (connection_id, asset_type, external_asset_id)
);
CREATE INDEX meta_assets_seller_idx ON meta_assets (seller_id);
CREATE INDEX meta_assets_external_idx ON meta_assets (asset_type, external_asset_id);

CREATE TABLE meta_credentials (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id          uuid NOT NULL UNIQUE REFERENCES meta_connections (id) ON DELETE CASCADE,
    -- AES-256-GCM (nonce || ciphertext || tag); NULL once revoked.
    encrypted_access_token bytea,
    key_version            text NOT NULL,
    token_type             text NOT NULL,
    expires_at             timestamptz,
    issued_at              timestamptz NOT NULL DEFAULT now(),
    last_validated_at      timestamptz,
    last_refreshed_at      timestamptz,
    revoked_at             timestamptz,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX meta_credentials_expiry_idx ON meta_credentials (expires_at) WHERE revoked_at IS NULL;
CREATE INDEX meta_credentials_key_version_idx ON meta_credentials (key_version);

CREATE TABLE meta_insights (
    id                  bigserial PRIMARY KEY,
    seller_id           text NOT NULL,
    connection_id       uuid NOT NULL REFERENCES meta_connections (id) ON DELETE CASCADE,
    asset_id            uuid NOT NULL REFERENCES meta_assets (id) ON DELETE CASCADE,
    platform            text NOT NULL,
    metric_name         text NOT NULL,
    metric_value        numeric,
    metric_unit         text NOT NULL DEFAULT 'count',
    aggregation_period  text NOT NULL,          -- day | lifetime | snapshot
    period_start        timestamptz NOT NULL,
    period_end          timestamptz NOT NULL,
    -- Stable text form of `dimensions` (breakdown values, media/post/template ID) for the unique key.
    dimension_key       text NOT NULL DEFAULT '',
    dimensions          jsonb NOT NULL DEFAULT '{}',
    source              text NOT NULL DEFAULT 'graph_api',   -- graph_api | webhook
    availability_status text NOT NULL DEFAULT 'available',
    fetched_at          timestamptz NOT NULL DEFAULT now(),
    metadata            jsonb NOT NULL DEFAULT '{}',
    CONSTRAINT meta_insights_unique UNIQUE
        (connection_id, asset_id, metric_name, aggregation_period, period_start, dimension_key)
);
CREATE INDEX meta_insights_seller_period_idx ON meta_insights (seller_id, platform, period_start, period_end);
CREATE INDEX meta_insights_connection_idx ON meta_insights (connection_id, metric_name);

-- Why a metric has no data (unsupported, permission_required, not_available, not_synced, expired_token).
CREATE TABLE meta_metric_status (
    connection_id       uuid NOT NULL REFERENCES meta_connections (id) ON DELETE CASCADE,
    metric_name         text NOT NULL,
    availability_status text NOT NULL,
    reason              text,
    checked_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (connection_id, metric_name)
);

CREATE TABLE meta_sync_jobs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id         text NOT NULL,
    connection_id     uuid NOT NULL REFERENCES meta_connections (id) ON DELETE CASCADE,
    job_type          text NOT NULL CHECK (job_type IN ('initial', 'incremental', 'manual', 'token_refresh')),
    status            text NOT NULL DEFAULT 'queued' CHECK (status IN
                      ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')),
    run_after         timestamptz NOT NULL DEFAULT now(),
    lease_until       timestamptz,
    started_at        timestamptz,
    completed_at      timestamptz,
    retry_count       integer NOT NULL DEFAULT 0,
    records_processed integer NOT NULL DEFAULT 0,
    last_error        text,          -- machine-readable code only, never a raw Meta payload
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
-- Sync lock: at most one queued/running job per connection.
CREATE UNIQUE INDEX meta_sync_jobs_active_idx ON meta_sync_jobs (connection_id) WHERE status IN ('queued', 'running');
CREATE INDEX meta_sync_jobs_due_idx ON meta_sync_jobs (status, run_after);
CREATE INDEX meta_sync_jobs_seller_idx ON meta_sync_jobs (seller_id, created_at DESC);

CREATE TABLE meta_webhook_events (
    id                  bigserial PRIMARY KEY,
    platform            text NOT NULL,
    event_id            text NOT NULL,     -- sha256 of the change; Meta retries resend identical changes
    external_account_id text,
    event_type          text,
    payload             jsonb NOT NULL,    -- minimized: no message bodies, no customer phone numbers
    received_at         timestamptz NOT NULL DEFAULT now(),
    processed_at        timestamptz,
    processing_status   text NOT NULL DEFAULT 'pending' CHECK (processing_status IN
                        ('pending', 'processing', 'processed', 'unmatched', 'ignored', 'failed')),
    attempts            integer NOT NULL DEFAULT 0,
    last_error          text,
    CONSTRAINT meta_webhook_events_dedupe UNIQUE (platform, event_id)
);
CREATE INDEX meta_webhook_events_status_idx ON meta_webhook_events (processing_status, received_at);
CREATE INDEX meta_webhook_events_account_idx ON meta_webhook_events (platform, external_account_id);

-- WhatsApp message status webhooks reduced to (hashed message ID, status, time) per connection.
CREATE TABLE meta_whatsapp_status_events (
    connection_id    uuid NOT NULL REFERENCES meta_connections (id) ON DELETE CASCADE,
    seller_id        text NOT NULL,
    phone_number_id  text NOT NULL,
    message_ref      text NOT NULL,         -- sha256 of the wamid, never the wamid or customer number
    status           text NOT NULL,         -- sent | delivered | read | failed
    status_at        timestamptz NOT NULL,
    pricing_category text,
    PRIMARY KEY (connection_id, message_ref, status)
);
CREATE INDEX meta_wa_status_seller_time_idx ON meta_whatsapp_status_events (seller_id, status_at);

CREATE TABLE meta_oauth_transactions (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id          text NOT NULL,
    platform           text NOT NULL,
    state_hash         text NOT NULL UNIQUE,     -- sha256(state); the state itself is never stored
    encrypted_verifier bytea,                    -- PKCE code_verifier, when used
    encrypted_code     bytea,                    -- authorization code captured by the callback
    key_version        text,
    status             text NOT NULL DEFAULT 'pending' CHECK (status IN
                       ('pending', 'authorized', 'consumed', 'denied', 'expired', 'failed')),
    error_code         text,
    expires_at         timestamptz NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    authorized_at      timestamptz,
    consumed_at        timestamptz
);
CREATE INDEX meta_oauth_transactions_seller_idx ON meta_oauth_transactions (seller_id, created_at DESC);
CREATE INDEX meta_oauth_transactions_expiry_idx ON meta_oauth_transactions (expires_at);

CREATE TABLE meta_audit_logs (
    id            bigserial PRIMARY KEY,
    seller_id     text,
    connection_id uuid,
    platform      text,
    action        text NOT NULL,
    outcome       text NOT NULL DEFAULT 'success',
    actor         text NOT NULL DEFAULT 'seller',  -- seller | system | admin | meta
    details       jsonb NOT NULL DEFAULT '{}',     -- redacted; never tokens, codes or message bodies
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX meta_audit_logs_seller_idx ON meta_audit_logs (seller_id, created_at DESC);
CREATE INDEX meta_audit_logs_connection_idx ON meta_audit_logs (connection_id, created_at DESC);
CREATE INDEX meta_audit_logs_created_idx ON meta_audit_logs (created_at);

CREATE TABLE meta_data_deletion_requests (
    confirmation_code text PRIMARY KEY,
    meta_user_hash    text NOT NULL,
    status            text NOT NULL DEFAULT 'completed',
    connections       integer NOT NULL DEFAULT 0,
    created_at        timestamptz NOT NULL DEFAULT now()
);
