-- Email integration schema (Gmail / Outlook mailboxes per seller). Applied once by app.core.pg.migrate.
-- Lives in the AI_python_backend database next to meta_* tables; every table is prefixed email_.

-- One row per seller + provider + mailbox address; reconnecting the same mailbox reuses the row.
CREATE TABLE email_connections (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id        text NOT NULL,
    provider         text NOT NULL CHECK (provider IN ('gmail', 'outlook')),
    account_email    text NOT NULL,
    display_name     text,
    status           text NOT NULL DEFAULT 'connected'
                     CHECK (status IN ('connected', 'reconnect_required', 'disconnected')),
    scopes           text[] NOT NULL DEFAULT '{}',
    connected_at     timestamptz NOT NULL DEFAULT now(),
    disconnected_at  timestamptz,
    last_used_at     timestamptz,
    last_error_code  text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT email_connections_seller_mailbox UNIQUE (seller_id, provider, account_email)
);
CREATE INDEX email_connections_seller_idx ON email_connections (seller_id);

-- Encrypted refresh token (AES-256-GCM, AAD = connection id). NULL after disconnect.
CREATE TABLE email_credentials (
    connection_id    uuid PRIMARY KEY REFERENCES email_connections (id) ON DELETE CASCADE,
    encrypted_refresh_token bytea,
    key_version      text,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- A seller-bound OAuth attempt. Only sha256(state) is stored; the PKCE verifier and the returned code are
-- encrypted. The browser callback only records the code; the seller's own request exchanges it.
CREATE TABLE email_oauth_transactions (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id             text NOT NULL,
    provider              text NOT NULL CHECK (provider IN ('gmail', 'outlook')),
    state_hash            text NOT NULL UNIQUE,
    encrypted_verifier    bytea NOT NULL,
    verifier_key_version  text NOT NULL,
    encrypted_code        bytea,
    code_key_version      text,
    status                text NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'authorized', 'denied', 'consumed')),
    expires_at            timestamptz NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX email_oauth_transactions_expiry_idx ON email_oauth_transactions (expires_at);

-- One row per draft attempt: the audit trail and the hourly limit. The email body is never stored.
CREATE TABLE email_draft_log (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id          text NOT NULL,
    user_id            text,
    connection_id      uuid REFERENCES email_connections (id) ON DELETE SET NULL,
    provider           text NOT NULL,
    from_email         text NOT NULL,
    report_id          text,
    subject            text NOT NULL,
    attachment_name    text,
    attachment_bytes   integer,
    attachment_sha256  text,
    status             text NOT NULL CHECK (status IN ('created', 'failed')),
    provider_draft_id  text,
    error_code         text,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX email_draft_log_seller_created_idx ON email_draft_log (seller_id, created_at DESC);

-- Connection lifecycle events (connected, reconnect required, disconnected, ...).
CREATE TABLE email_audit_logs (
    id             bigserial PRIMARY KEY,
    seller_id      text NOT NULL,
    connection_id  uuid,
    action         text NOT NULL,
    details        jsonb NOT NULL DEFAULT '{}',
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX email_audit_logs_seller_idx ON email_audit_logs (seller_id, created_at DESC);
