-- AI Assistant chat: saved conversations, every AI call's token usage (for charges) and per-user limits.
-- Applied once by app.core.pg.migrate. Lives in the AI_python_backend database; every table is prefixed assistant_.

-- One conversation of one user (the BigShip user ID sent by the panel as X-User-Id).
CREATE TABLE assistant_sessions (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     text NOT NULL,
    title       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX assistant_sessions_user_idx ON assistant_sessions (user_id, updated_at DESC);

-- Messages of a conversation, oldest first. Deleting the conversation deletes them.
CREATE TABLE assistant_messages (
    id            bigserial PRIMARY KEY,
    session_id    uuid NOT NULL REFERENCES assistant_sessions (id) ON DELETE CASCADE,
    role          text NOT NULL CHECK (role IN ('user', 'assistant')),
    content       text NOT NULL,
    -- Assistant answers: [{"kind": "text"|"copy", "text": ...}] as shown in the panel.
    parts         jsonb,
    -- User messages: names and types of the attached files, and their text so follow-up questions can use it.
    files         jsonb NOT NULL DEFAULT '[]',
    file_context  text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX assistant_messages_session_idx ON assistant_messages (session_id, id);

-- One row per AI call. Kept when the conversation is deleted (no foreign key): this is the billing record.
CREATE TABLE assistant_usage (
    id                 bigserial PRIMARY KEY,
    user_id            text NOT NULL,
    session_id         uuid,
    message_id         bigint,
    endpoint           text NOT NULL,            -- 'chat' or 'ask'
    model              text NOT NULL,
    prompt_tokens      integer NOT NULL CHECK (prompt_tokens >= 0),
    completion_tokens  integer NOT NULL CHECK (completion_tokens >= 0),
    total_tokens       integer NOT NULL CHECK (total_tokens >= 0),
    -- true when the AI service did not report usage and it was estimated from the text length.
    estimated          boolean NOT NULL DEFAULT false,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX assistant_usage_user_time_idx ON assistant_usage (user_id, created_at);

-- Optional per-user limits; NULL (or no row) uses the server default (AI_ASSISTANT_*_TOKEN_LIMIT), 0 = no limit.
CREATE TABLE assistant_limits (
    user_id         text PRIMARY KEY,
    daily_tokens    integer CHECK (daily_tokens >= 0),
    weekly_tokens   integer CHECK (weekly_tokens >= 0),
    monthly_tokens  integer CHECK (monthly_tokens >= 0),
    note            text,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
