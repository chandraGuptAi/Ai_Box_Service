# Meta Integration (WhatsApp Business, Instagram, Facebook Pages)

The `meta_integration` module lets each BigShip seller connect the Meta business accounts they own:
- WhatsApp Business Account through Embedded Signup v4
- Instagram professional account through Instagram Login
- Facebook Pages through Facebook Login for Business

A background worker syncs each account's insights into PostgreSQL. The AI Box report engine then reads
those normalized metrics from `/api/meta/report-context`.

> **Status: integration-ready, not production-verified.** Every Meta call is built from Meta's documentation
> (checked November 2026, Graph API v27.0) and tested against a mocked Graph API and a mocked BigShip
> profile API. The gateway proxy, BigShip-verified seller identity, Meta data in AI reports and the Angular
> UI are done. Still required before production: the Meta app's credentials in `.env` (see
> [Meta Dashboard and local setup](#meta-dashboard-and-local-setup)), App Review, Business Verification and,
> for WhatsApp, Tech Provider status.

## Architecture

```
Angular /ai_box/create ──► AI_python_backend (gateway: service token; BigShip login forwarded)
                                 │  /api/meta/*  (proxied)
                                 ▼
                        Ai_Box_Service  meta_integration
   ┌──────────────┬───────────────┬──────────────┬──────────────────────┐
   router.py   services/        providers/      worker.py           repository.py
   (API)       connections      whatsapp        (jobs, webhooks,    (all SQL,
               sync             instagram        schedule, purge)    seller-scoped)
               webhooks         facebook
               reporting        graph.py (shared HTTP client)
                                 │
                                 ▼
             PostgreSQL: the AI_python_backend database, meta_* tables
                                 │
                                 ▼
       POST /api/meta/report-context ──► AI_python_backend report engine
```

| File | Responsibility |
|---|---|
| `config.py` | `META_*` settings; per-platform readiness checks |
| `errors.py` | Machine-readable error codes and the error response body |
| `security.py` | AES-256-GCM token encryption with key versions, webhook/`signed_request` HMAC, log redaction |
| `db.py`, `migrations/` | Connection pool and the SQL migration runner (advisory-locked) |
| `repository.py` | Every SQL statement; seller-facing queries always filter on `seller_id` |
| `providers/` | One adapter per platform (`BaseMetaProvider`), plus the shared Graph client |
| `services/connections.py` | Start, callback, complete, disconnect, manual sync, deauthorize |
| `services/sync.py` | Runs jobs, maps errors to connection states, backoff, token refresh, key rotation |
| `services/webhooks.py` | Signature check, minimization, dedupe, attribution |
| `services/reporting.py` | Report data source: freshness, coverage, unavailable-metric reasons |
| `worker.py` | Background loop; can also run as a separate process |
| `identity.py` | Which seller is calling: asks BigShip whose login token the request carries |

The module only imports from `app.core`. It is registered in `modules.yaml` with `prefix: /meta`.

### Startup when Meta is not configured

The module needs a database URL and encryption keys to start. If they are missing or the database is
unreachable, it does the following and the rest of the service keeps working:
- logs the reason;
- sets `app.state.meta = None`;
- answers its endpoints with `META_PLATFORM_NOT_CONFIGURED`.

Shipment, attachment and report features are unaffected.

## Database

The tables live in the **same PostgreSQL database as `AI_python_backend`**:
- Set `DATABASE_URL` to the same value used there. `META_DATABASE_URL` overrides it if set.
- Every table is prefixed `meta_`, so there is no clash with `saved_reports` or `canonical_reports`.
- The module owns its migrations: `migrations/NNNN_*.sql` files are tracked in `meta_schema_migrations`
  and applied on startup under `pg_advisory_xact_lock`. Several processes can start at once safely.

| Table | Purpose |
|---|---|
| `meta_connections` | One row per seller + platform + Meta account. Status: `pending`, `connected`, `expired`, `reconnect_required`, `disconnected`, `failed`. Unique `(seller_id, platform, external_account_id)`. |
| `meta_assets` | WABA, phone numbers, IG account, Page, with metadata |
| `meta_credentials` | Encrypted token plus `key_version`, expiry and validation times. The token column is set to NULL on revoke. |
| `meta_insights` | Normalized metrics. Unique `(connection, asset, metric, period, period_start, dimension_key)` makes upserts idempotent. |
| `meta_metric_status` | Why a metric is missing: `unsupported`, `permission_required`, `not_available`, `not_synced`, `expired_token` |
| `meta_sync_jobs` | Job queue. A partial unique index allows one queued/running job per connection; this is the sync lock. |
| `meta_webhook_events` | Minimized webhook changes, unique `(platform, event_id)` |
| `meta_whatsapp_status_events` | Read/failed counts. Each row holds a hashed message ID, status and time. |
| `meta_oauth_transactions` | Seller-bound connection attempts. Stores only `sha256(state)`, an encrypted code and an encrypted PKCE verifier. |
| `meta_audit_logs` | Connection started/completed/failed, permissions changed, token invalidated, disconnected, sync started/completed/failed/retry, access denied |
| `meta_data_deletion_requests` | Confirmation codes for Meta data deletion callbacks |

PostgreSQL 13+ is required for `gen_random_uuid()`; the gateway runs 17.

**Docker.** `AI_python_backend`'s compose file reaches its database at host `postgres` inside its own
network. Point this service at the same server:
- Same host: publish Postgres to the host (the local setup uses `127.0.0.1:5434`).
- Containers: attach both services to one Docker network and use the `postgres` hostname.

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` / `META_DATABASE_URL` | yes | The `AI_python_backend` database |
| `META_TOKEN_ENCRYPTION_KEYS` | yes | JSON `{"1": "<base64 32 bytes>"}`; every listed key can decrypt |
| `META_TOKEN_ENCRYPTION_ACTIVE_KEY` | yes | Key version used for new encryption |
| `META_GRAPH_API_VERSION` | yes | No default on purpose, e.g. `v27.0`. Graph versions retire about 2 years after release (v21.0 is removed Jan 2027). |
| `META_APP_ID`, `META_APP_SECRET` | WhatsApp, Facebook | The single central BigShip Meta app |
| `META_WHATSAPP_CONFIG_ID` | WhatsApp | Facebook Login for Business configuration for Embedded Signup |
| `META_FACEBOOK_CONFIG_ID` | Facebook | Facebook Login for Business configuration for Page access |
| `META_INSTAGRAM_CONFIG_ID` | Instagram | Facebook Login for Business configuration with Instagram assets (`instagram_basic`, `instagram_manage_insights`, `pages_show_list`). When set, Instagram uses `META_APP_ID`/`SECRET` and graph.facebook.com. |
| `META_INSTAGRAM_APP_ID`, `META_INSTAGRAM_APP_SECRET` | Instagram (only without `META_INSTAGRAM_CONFIG_ID`) | App Dashboard → Instagram → API setup with Instagram login |
| `META_OAUTH_REDIRECT_URI` | Facebook, Instagram | `https://<gateway>/api/meta/oauth/callback`. Must be https when `ENV=production`. |
| `META_FRONTEND_RETURN_URL` | Facebook, Instagram | Angular page the callback returns to, e.g. `https://<panel>/ai_box/create` |
| `META_PUBLIC_API_BASE_URL` | recommended | `https://<gateway>/api/meta`, used for the data-deletion status URL |
| `META_WEBHOOK_VERIFY_TOKEN` | webhooks | Random string entered in the App Dashboard |
| `META_WEBHOOK_CALLBACK_URL` | docs only | `https://<gateway>/api/meta/webhook` |
| `META_SYNC_ENABLED` / `META_SYNC_INTERVAL_MINUTES` | no | Defaults `true` / `30` |
| `META_INITIAL_SYNC_DAYS` / `META_INCREMENTAL_SYNC_DAYS` | no | Defaults `30` / `3` |
| `META_WORKER_ENABLED` | no | Default `true` (worker runs in the API process). Set `false` when running a separate worker. |
| `META_HTTP_TIMEOUT_SECONDS` | no | Default `20` |
| `META_FACEBOOK_PKCE` | no | Default `false`. Enable only after verifying PKCE with your app. |
| `META_ALLOW_SHARED_ACCOUNTS` | no | Default `false`: one Meta account can belong to one seller. |
| `META_ADMIN_API_KEY` | no | Enables `/api/meta/admin/*` through `X-Meta-Admin-Key` |
| `META_SELLER_IDENTITY` | no | `bigship` (default): the seller is whoever BigShip says owns the request's login token. `header`: trust `X-User-Id`; local development only, refused when `ENV=production`. |
| `BIGSHIP_API_BASE_URL` / `META_BIGSHIP_API_BASE_URL` | with `bigship` | The BigShip API that answers `GET /api/get-user-profile` (already set for the other modules) |
| `META_SELLER_IDENTITY_CACHE_SECONDS` | no | Default `300`. How long a verified token → seller mapping is reused (keyed by the token's SHA-256). |
| `META_STALE_AFTER_MINUTES`, `META_WEBHOOK_RETENTION_DAYS`, `META_AUDIT_RETENTION_DAYS`, `META_SELLER_RATE_LIMIT_PER_MINUTE`, `META_MANUAL_SYNC_MIN_INTERVAL_SECONDS` | no | Defaults 180, 30, 365, 20, 300 |

Secrets stay on this server:
- No access token, code, App Secret or encryption key is ever returned by an API or written to logs.
- httpx request logs are filtered: `access_token`, `appsecret_proof`, `client_secret` and `code` are replaced
  with `[REDACTED]`.

## Meta setup

### 1. Meta developer app and Business Portfolio
1. Create one **Business** type app under BigShip's **verified** Meta Business Portfolio.
2. Add the products **WhatsApp**, **Facebook Login for Business**, **Instagram** (API setup with Instagram
   login) and **Webhooks**.
3. App settings → Basic: set the Privacy Policy URL, then the data URLs:
   - Data deletion: `https://<gateway>/api/meta/data-deletion`
   - Deauthorize callback (Facebook Login for Business settings): `https://<gateway>/api/meta/deauthorize`
4. Register `META_OAUTH_REDIRECT_URI` exactly in both places:
   - Facebook Login for Business → Valid OAuth Redirect URIs
   - Instagram → Business login settings
5. Use a separate app (or test app) for each environment: development, staging and production.

### 2. WhatsApp: Embedded Signup v4 (Tech Provider)
Meta deprecated Embedded Signup v2 and v3 on Oct 15 2026, so v4 is required.
1. Complete Tech Provider onboarding (or Solution Partner, if BigShip will share its credit line).
2. Facebook Login for Business → Configurations → create a **WhatsApp Embedded Signup** configuration.
   Select assets **WhatsApp accounts** and permissions `whatsapp_business_management` (plus
   `whatsapp_business_messaging` if messaging is planned). Put its ID in `META_WHATSAPP_CONFIG_ID`.
3. Add the panel domain to *Allowed domains* for the JavaScript SDK.
4. Webhooks: subscribe the app to the `whatsapp_business_account` object with fields `messages` and
   `account_update`. The module subscribes each seller's WABA itself (`POST /{waba}/subscribed_apps`).

What the server does on `complete`:
1. Exchanges the code. The code has a 30-second lifetime and the exchange sends no `redirect_uri`.
2. Calls `debug_token` and requires the claimed WABA to be one of the token's
   `granular_scopes[whatsapp_business_management].target_ids`.
3. Lists `/{waba}/phone_numbers` and requires the claimed phone number to be one of them.
4. Subscribes the app to the WABA's webhooks.
5. Registers the phone number with `/{phone}/register` only if the seller supplied a 6-digit PIN and the flow
   was `FINISH`. Reporting does not need registration. The PIN is not stored.

Coexistence: `FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING` is accepted and registration is skipped, as Meta
requires. Contact and history sync for coexistence is **not** implemented; it is not needed for reporting.
Personal WhatsApp numbers cannot connect. There is no QR or WhatsApp Web automation. The existing
escalation service, which uses QR login, is separate and untouched.

### 3. Instagram: Instagram API with Instagram Login
This flow was chosen because the reports need only account and media insights, and Instagram Login does
not require a linked Facebook Page.
- Scopes: `instagram_business_basic`, `instagram_business_manage_insights`.
- Personal accounts are rejected (`META_ACCOUNT_NOT_ELIGIBLE`). `account_type` must be `BUSINESS` or
  `MEDIA_CREATOR`.
- The short-lived token is exchanged for a 60-day token. The worker refreshes it once it is at least 24 hours
  old and expires within 10 days.

### 4. Facebook Pages: Facebook Login for Business
- Create a Facebook Login for Business configuration with permissions `pages_show_list`,
  `pages_read_engagement` and `read_insights`, and put its ID in `META_FACEBOOK_CONFIG_ID`.
- The seller picks Pages in Meta's dialog. Each Page with the **ANALYZE** task becomes its own connection.
  Other Pages come back in `skipped` with the reason `analyze_task_required`.
- Page tokens come from a long-lived user token and do not expire. They can still be invalidated, which the
  worker detects.

### 5. App Review
Request Advanced Access only for the permissions listed above:
- `whatsapp_business_management`
- `pages_show_list`, `pages_read_engagement`, `read_insights`
- `instagram_business_basic`, `instagram_business_manage_insights`

Business Verification is required. Explain in the review that the data is aggregated insights for the
seller's own reports and that no message content is stored.

### 6. Webhooks
- Callback URL: `https://<gateway>/api/meta/webhook`
- Verify token: `META_WEBHOOK_VERIFY_TOKEN`
- Every POST must carry a valid `X-Hub-Signature-256` over the raw body, checked against the App Secret
  (or the Instagram app secret) in constant time. Invalid → 401.
- Valid events are stored and acknowledged immediately; the worker processes them later.
- No Instagram or Page webhook fields are subscribed, because no implemented feature needs them.

**Local development:** see [Meta Dashboard and local setup](#meta-dashboard-and-local-setup).

## Meta Dashboard and local setup

### Local ports

| Port | Component |
|---|---|
| 4200 | Angular panel (`ng serve`) |
| 8200 | `AI_python_backend` gateway. The panel's `environment.aiBoxApiUrl` points here, and it proxies `/api/meta/*`. |
| 8100 | Ai_Box_Service (`AI_BOX_SERVICE_URL` in the gateway's `.env`) |

Meta never talks to Ai_Box_Service directly. The OAuth callback, webhooks, deauthorize and data deletion all
go to the gateway's public URL.

### 1. Public HTTPS URL for the gateway
Meta calls back over HTTPS, so the gateway needs a public HTTPS URL. Start a tunnel yourself; nothing in
this repo opens one:

```bash
cloudflared tunnel --url http://localhost:8200      # or: ngrok http 8200
```

Call the result `https://<tunnel>`. Free tunnel hostnames change on every start, so each restart means
updating `.env` and the Dashboard.

### 2. Ai_Box_Service `.env` (you fill these; nothing is generated)

```dotenv
META_APP_ID=                 # App settings → Basic → App ID
META_APP_SECRET=             # App settings → Basic → App secret
META_GRAPH_API_VERSION=v27.0
META_WHATSAPP_CONFIG_ID=     # Facebook Login for Business → Configurations (WhatsApp Embedded Signup)
META_FACEBOOK_CONFIG_ID=     # Facebook Login for Business → Configurations (Pages)
META_INSTAGRAM_CONFIG_ID=    # Facebook Login for Business → Configurations (Instagram); preferred
META_INSTAGRAM_APP_ID=       # only without the config ID: Instagram → API setup with Instagram login → Instagram app ID
META_INSTAGRAM_APP_SECRET=   # same page → Instagram app secret
META_OAUTH_REDIRECT_URI=https://<tunnel>/api/meta/oauth/callback
META_FRONTEND_RETURN_URL=http://localhost:4200/ai_box/create   # http is accepted only when ENV != production
META_PUBLIC_API_BASE_URL=https://<tunnel>/api/meta
META_WEBHOOK_VERIFY_TOKEN=   # any random string; type the same value in the Dashboard
META_WEBHOOK_CALLBACK_URL=https://<tunnel>/api/meta/webhook   # for reference only
```

- `DATABASE_URL`, `META_TOKEN_ENCRYPTION_KEYS`, `META_TOKEN_ENCRYPTION_ACTIVE_KEY` and
  `BIGSHIP_API_BASE_URL` are already set locally.
- Restart Ai_Box_Service after editing, because `.env` is read at startup.
  `GET http://127.0.0.1:8200/api/meta/platforms` then shows `configured: true` and an empty
  `missing_configuration` for each platform.

**Instagram login mode:**
- With `META_INSTAGRAM_CONFIG_ID` set, Instagram connects through Facebook Login for Business on the main
  app (Unified login). The Instagram account must be a professional account **linked to a Facebook Page**;
  the linked Page's token (does not expire) is stored, or the 60-day user token when no Page token is shared.
  Use this when the app cannot add "API setup with Instagram login" (newer apps).
- Without it, the Instagram Login flow below is used.

**Instagram app ID (Instagram Login mode only):**
- Instagram API with Instagram Login uses the **Instagram app ID and secret** shown on the Instagram product
  page. These are different from the Meta App ID, so `META_INSTAGRAM_APP_ID` cannot reuse `META_APP_ID`.
- This flow needs **no Facebook Page**. It does need an Instagram **professional** (Business or Creator)
  account. Personal accounts are rejected with `META_ACCOUNT_NOT_ELIGIBLE` and have no insights.

### 3. Meta App Dashboard (app `Ai_box`)

| Where | Setting |
|---|---|
| App settings → Basic | App domains: the tunnel host (and later the production gateway and panel hosts). Privacy Policy URL. Data deletion: `https://<tunnel>/api/meta/data-deletion`. |
| Facebook Login for Business → Settings | Valid OAuth Redirect URIs: `https://<tunnel>/api/meta/oauth/callback` (exact match). Login with the JavaScript SDK: on. Allowed domains for the JavaScript SDK: the panel's origin. Deauthorize callback: `https://<tunnel>/api/meta/deauthorize`. |
| Facebook Login for Business → Configurations | **WhatsApp:** login variation *WhatsApp Embedded Signup*, asset *WhatsApp accounts*, permissions `whatsapp_business_management` (+ `whatsapp_business_messaging` only if messaging is planned) → `META_WHATSAPP_CONFIG_ID`. **Facebook Pages:** asset *Pages*, permissions `pages_show_list`, `pages_read_engagement`, `read_insights` → `META_FACEBOOK_CONFIG_ID`. |
| Instagram → API setup with Instagram login | Business login settings → OAuth redirect URIs: the same `https://<tunnel>/api/meta/oauth/callback`. Copy the Instagram app ID and secret. |
| Webhooks | Product *WhatsApp Business Account*: callback `https://<tunnel>/api/meta/webhook`, verify token `META_WEBHOOK_VERIFY_TOKEN`, fields `messages`, `account_update`. Meta sends the verification GET as soon as you save, so the tunnel and both services must be running. |

### 4. Who can log in while the app is unpublished
- In development mode, only people with a role on the app (admin, developer or tester) can authorize it.
- **Instagram:** the Instagram account has to be added under App roles → Roles → Instagram Testers, and the
  invitation accepted in Instagram's settings.
- **Everyone else:** sellers outside these roles need App Review (Advanced Access for the permissions above),
  Business Verification and a published app.

### 5. WhatsApp requirements
- Embedded Signup runs inside a Meta **Business Portfolio**:
  - The app must belong to BigShip's verified portfolio.
  - The seller selects or creates their own portfolio in the signup window.
- Onboarding businesses other than BigShip needs **Tech Provider** status (or Solution Partner).
- Until then, test with a WABA you administer.
- **Test numbers:** the free test number on WhatsApp → API Setup belongs to a test account Meta creates for the
  app. This module only connects accounts through Embedded Signup, so test the connect flow with a number you
  control.
- **Data access:**
  - Reporting does not need the number to be registered for messaging.
  - The module never reads personal WhatsApp chats or message text.

### 6. Browser rules for local testing
- Meta's JavaScript SDK (`FB.login`, used for WhatsApp signup) may refuse to run on an `http://` page. If it
  does, serve the panel over HTTPS (`ng serve --ssl`, or a second tunnel to port 4200) and add that origin to
  *Allowed domains for the JavaScript SDK*.
- Instagram and Facebook use a popup that returns to `META_FRONTEND_RETURN_URL`. Allow popups for the panel
  origin.
- Use one browser profile that is logged in to both the panel (BigShip) and Facebook or Instagram.

### 7. Webhook testing
- After saving the callback, use the Dashboard's **Test** button next to a field. Without a matching
  connected WABA, the event is stored and acknowledged as `unmatched`; this is expected.
- A 401 response means `META_APP_SECRET` is wrong or something rewrote the request body. The tunnel and
  gateway forward it unchanged.

### 8. What "configured" means
- `GET /api/meta/platforms` → `configured: true` means only that the local settings are present.
- Whether Meta accepts the app, the permissions and the seller's account shows up on the first real
  connection, and the panel reports connection errors as they come.
- A platform shows **Connected** only after a real authorization succeeds and `/connections` reports it.

## Data collected

All metrics are aggregates; message bodies and customer identifiers are never stored.

| Platform | Metrics | Source |
|---|---|---|
| WhatsApp | `messages_sent`, `messages_delivered` (daily) | `analytics` field. `conversation_analytics` is not used: Meta removes it on Nov 1 2026. |
| | `pricing_volume` by pricing category | `pricing_analytics` with metric `VOLUME` |
| | `template_sent` / `delivered` / `read` / `clicked` per template | `template_analytics` (90-day lookback). Only works if the seller enabled template insights in WhatsApp Manager. The module **never** enables it: enabling cannot be undone. |
| | `status_read`, `status_failed` | Status webhooks received since connection. Read count is a lower bound (recipients can disable read receipts). |
| Instagram | `reach`, `accounts_engaged` (not additive); `views`, `total_interactions`, `likes`, `comments`, `shares`, `saves`, `replies`, `profile_links_taps`, `follows`, `unfollows` (daily) | `/{ig}/insights`. `impressions` is retired. Data may be delayed up to 48 h. |
| | `followers_count`, `media_count` (snapshot); `media_*` per post/reel | `/me`, `/{media}/insights` |
| Facebook | `page_media_view`, `page_post_engagements`, `page_views_total`, `page_daily_follows_unique`, `page_daily_unfollows_unique` (additive); `page_total_media_view_unique`, `page_follows` (not additive) | `/{page}/insights`, max 90 days per request. `page_impressions*` and `page_fans*` were retired Nov 2025 and are not requested. |
| | `post_media_view`, `post_total_media_view_unique`, `post_clicks`, `post_reactions` by type | `/{post}/insights` |

How the providers handle Meta's quirks:
- **Rejected metric:** Meta fails the whole request if one metric is invalid. The provider retries metric by
  metric, stores the good ones and records the rejected one as `unsupported`.
- **No data:** an empty response is never turned into zeros. The metric is reported as unavailable with a
  reason.
- **Instagram follows:** the `follows_and_unfollows` breakdown is mapped as `FOLLOWER` → follows and
  `NON_FOLLOWER` → unfollows. Re-check this mapping against live data.

## Sync

There is no Redis or Celery in this stack, so PostgreSQL is the queue.

- **Claiming:** a worker claims a job with `FOR UPDATE SKIP LOCKED`.
- **Sync lock:** the partial unique index allows one active job per connection.
- **Leases:** a job is leased for `META_SYNC_LEASE_SECONDS`; expired leases are re-queued.
- **Job types:**
  - `initial`: 30 days, queued automatically on connect.
  - `incremental`: 3 days, every `META_SYNC_INTERVAL_MINUTES`.
  - `manual`: `POST .../sync`.
  - `token_refresh`: Instagram only.
- **Health check:** each sync first re-lists the account's assets. This catches revoked Pages and removed
  phone numbers.
- **Errors:**

| Error | Result |
|---|---|
| Token expired (190/463) | Connection `expired` |
| Token invalid or permission lost | Connection `reconnect_required`, audited |
| Rate limited (4/17/32/613/800xx/130429) or Meta 5xx | Retried with exponential backoff (1 min → 1 h, honoring `estimated_time_to_regain_access`), up to `META_SYNC_MAX_RETRIES`, then `failed` |

- Upserts are idempotent: re-syncing never duplicates rows.
- **Separate worker process:** set `META_WORKER_ENABLED=false` on the API and run
  `python -m app.modules.meta_integration.worker` (any number of copies).
- Each platform syncs on its own schedule. Every report states `last_successful_sync_at` and `data_stale`.

## API (`/api/meta`)

Every route requires the gateway's `X-Ai-Box-Service-Token`, the same as every `/api` route. Errors look like
this:

```json
{"detail": {"code": "META_NOT_CONNECTED", "message": "Connect your WhatsApp Business account to use this data.",
            "platform": "whatsapp", "action": "connect", "retryable": false}}
```

`action` tells the UI what to offer: `connect`, `reconnect`, `retry_later` or `contact_support`.

**Error codes:**
- Connection state: `META_NOT_CONNECTED`, `META_TOKEN_EXPIRED`, `META_RECONNECT_REQUIRED`,
  `META_CONNECTION_NOT_FOUND`, `META_JOB_NOT_FOUND`
- Authorization: `META_AUTHORIZATION_FAILED`, `META_PERMISSION_DENIED`, `META_TRANSACTION_INVALID`
- Eligibility: `META_ACCOUNT_NOT_ELIGIBLE`, `META_ACCOUNT_CONNECTED_ELSEWHERE`
- Limits: `META_RATE_LIMITED`, `META_SELLER_RATE_LIMITED`
- Sync and data: `META_SYNC_FAILED`, `META_SYNC_IN_PROGRESS`, `META_METRIC_UNAVAILABLE`
- Server and request: `META_PLATFORM_NOT_CONFIGURED`, `META_PLATFORM_UNSUPPORTED`, `META_PROVIDER_ERROR`,
  `META_INVALID_REQUEST`

Raw Meta errors are never returned. Meta's `fbtrace_id` is logged server-side instead.

### Seller endpoints (`X-Shipment-Authorization` = the seller's own BigShip login)

Errors specific to identity: `META_SELLER_UNAUTHENTICATED` (401, `action: login`) when the login is missing,
expired or rejected by BigShip; `META_IDENTITY_UNAVAILABLE` (503, retryable) when BigShip can't be reached.

| Method & path | Purpose |
|---|---|
| `GET /platforms` | Readiness of each platform; `missing_configuration` lists setting names, never values. Works without a login. With one, each platform also has `seller_connected` and `connection_status` (`not_connected`, `pending`, `connected`, `expired`, `reconnect_required`, `failed`), and `seller_verified: true`. `configured: true` only means the local settings are present, not that Meta approved the app or that a seller authorized it. |
| `GET /connections` | The seller's connections: `connection_id`, `platform`, `account_name`, `connection_status`, `reconnect_required`, `last_sync_at`, `last_successful_sync_at`, `data_stale`, `available_features`, `active_sync_job_id` |
| `GET /connections/{id}` | The above plus `permission_scopes`, `assets`, and per-metric availability |
| `DELETE /connections/{id}?delete_data=false` | Disconnects: unsubscribes WABA webhooks, deletes the token, cancels jobs. History is kept unless `delete_data=true`. |
| `POST /connections/{id}/sync` | 202 with the job; returns the already-queued job if there is one (`X-Meta-Sync-Existing: true`) |
| `GET /sync-jobs/{job_id}` | `job_id`, `status`, `started_at`, `completed_at`, `records_processed`, `error_code`, `retry_count`, `next_attempt_at` |
| `POST /connect/{platform}/start` | Creates a transaction (10-minute TTL). WhatsApp gets `embedded_signup` SDK options; Facebook and Instagram get `authorization_url`. |
| `POST /connect/{platform}/complete` | `{transaction_id}`. WhatsApp also sends `code`, `waba_id`, `phone_number_id`, `business_id`, `event`, optional `pin`. |
| `GET /connect/transactions/{id}` | Status of a connection attempt |
| `POST /report-context` | `{data_sources, from_date, to_date, timezone}`; see below |

### Meta-facing endpoints (authenticated by state or signature, no seller login)
- `GET /oauth/callback`
- `GET|POST /webhook`
- `POST /deauthorize`
- `POST /data-deletion`
- `GET /data-deletion/{code}`

### Admin endpoints (`X-Meta-Admin-Key`)
- `GET /admin/connections`
- `GET /admin/audit-logs`
- `POST /admin/rotate-keys`

The full OpenAPI schema is at `/docs` and `/openapi.json`, under the **Meta Integration** tag.

### Example: `GET /connections`
```json
{"connections": [{"connection_id": "9b6f…", "platform": "whatsapp", "account_name": "Seller WABA",
  "external_account_id": "1000000000000001", "connection_status": "connected", "reconnect_required": false,
  "connected_at": "2026-11-17T06:40:00Z", "last_sync_at": "2026-11-17T07:10:02Z",
  "last_successful_sync_at": "2026-11-17T07:10:02Z", "data_stale": false, "last_error_code": null,
  "available_features": ["account", "category", "template"], "active_sync_job_id": null}]}
```

## Security model

**Seller identity**
- BigShip logins are Laravel Sanctum tokens (opaque `id|secret`, no claims to read), so the module asks
  BigShip: `GET {BIGSHIP_API_BASE_URL}/api/get-user-profile` with the request's `X-Shipment-Authorization`.
  That route is behind `auth:sanctum` and answers for the token's own account. The seller is
  `data.user_profile_data.cust_id`, the effective seller's CustomerId; for a sub-user that is the parent
  seller, so sub-users share the seller's Meta connections.
- `X-User-Id` is **ignored** for authorization. Changing it cannot reach another seller's connections; a
  mismatch is logged without values.
- Results are cached by the token's SHA-256 for `META_SELLER_IDENTITY_CACHE_SECONDS`. The token itself is
  never stored or logged.
- 401, 403 or 404 from BigShip, or `status: Failed`, means the login was rejected (`META_SELLER_UNAUTHENTICATED`).
  A network error, 5xx or unexpected body is retryable (`META_IDENTITY_UNAVAILABLE`); it never logs the seller out.
- `META_SELLER_IDENTITY=header` (trust `X-User-Id`) exists for local development without a BigShip login. With
  `ENV=production` it is reported as a configuration problem and no platform is ready.
- Every `/api` call must also carry the gateway's service token; the module refuses to start with
  `ENV=production` and an empty `SERVICE_TOKEN`.
- **Elsewhere:** other `AI_python_backend` features (saved reports, attachments) still key on `X-User-Id`
  from the browser. This module no longer depends on it.

**Tenant isolation**
- Every seller-facing query filters on `seller_id`.
- Another seller's connection or job returns 404, the same as one that doesn't exist. The attempt is
  audited as `access_denied`.

**Connection attempts**
- `start` binds a random `state` (only its SHA-256 is stored) to the seller.
- The callback only attaches the encrypted code; `complete` must come from the same seller and consumes the
  transaction exactly once.
- This blocks login CSRF and code replay, and the code never reaches the browser.

**Accepting browser-supplied account IDs**
- A WABA must appear in the token's granular targets, and a phone number must belong to that WABA.
- Pages come only from `/me/accounts`, and must have the ANALYZE task.
- Instagram accounts come only from `/me`.

**Tokens**
- AES-256-GCM, with the connection ID as associated data: a copied ciphertext won't decrypt on another row.
- Keys are versioned. **To rotate:**
  1. Add a new key and make it active.
  2. Call `POST /admin/rotate-keys`.
  3. Remove the old key.
- `appsecret_proof` is sent on every graph.facebook.com call.

**Webhooks**
- HMAC is checked over the raw body.
- Retries are deduped by content hash.
- Events are attributed only through stored assets: the WABA and the phone number must both belong to a
  connected account.
- Payloads are minimized: no message text, no customer numbers, message IDs hashed. Retention is 30 days;
  status rows are kept 400 days.

**Rate limits and audit**
- 20 connect/sync calls per seller per minute, and at least 5 minutes between manual syncs.
- Audit logs never contain tokens, codes, PINs or message bodies.

## Reconnection and token maintenance

| Situation | What the UI does |
|---|---|
| `reconnect_required: true` (status `expired` / `reconnect_required`) | Run `start` → `complete` again. The same connection row is reused and the history is kept. |
| Seller removes the app in Meta (`/deauthorize`) | Nothing: connections become `disconnected` and the tokens are deleted |
| Meta data deletion callback | Nothing: same as above, plus the metrics are deleted. Meta receives a status URL. |
| WhatsApp `PARTNER_REMOVED` / `ACCOUNT_DELETED` webhook | Connection becomes `reconnect_required` |

## Gateway integration (`AI_python_backend`)

Every browser call goes through the gateway. All steps are **done**:
- Step 2 is solved inside this module (see [Seller identity](#security-model)).
- Steps 3–6 are `app/models/report_schema.py` (`data_sources`), `app/services/meta_source.py` (client),
  `app/services/meta_reports.py` (report builder) and the hook in `report_service._generate_report`.
  Tests: `tests/test_meta_reports.py`.
- What the report does:
  - Every figure is a synced value from `report-context`.
  - Each account gets its own section: a freshness note, a metrics table (totals only for additive metrics), the
    top posts, a "Not included" list with reasons, and a daily trend chart.
  - The optional AI observations see aliased sources only ("WhatsApp Business account 1"), never names or IDs. An
    observation is kept only if every number it quotes is one of the supplied values.
  - With `shipments` also selected, the unchanged shipment pipeline runs and both are shown side by side
    (`report_type: combined`).

The original plan, for reference:

1. **Proxy (done):** `ServiceRoute("MetaIntegration", "/api/meta", …)` in `app/api_gateway.py`, using the
   existing `AI_BOX_SERVICE_URL` / `AI_BOX_SERVICE_TOKEN`; tested in `tests/test_api_gateway.py`.
   - The streaming proxy forwards the raw body and `X-Hub-Signature-256` unchanged, as webhook checks
     require.
   - Any `X-Ai-Box-Service-Token` sent by the caller is replaced.
   - The gateway adds no login check of its own, so Meta can reach `/oauth/callback`, `/webhook`,
     `/deauthorize` and `/data-deletion`. Seller routes need `X-User-Id`, which the panel's
     `AiBoxIdentityInterceptor` adds.
2. **Seller identity:** set `X-User-Id` from the verified session. See the security gap above.
3. **Report request (backward compatible):** add `data_sources: list[Literal["shipments","whatsapp","instagram","facebook"]] | None = None`
   to `ReportRequest`.
   - Requests without it behave exactly as today.
   - `["shipments"]` alone is the existing shipment pipeline.
4. **Fetch branch:** in `report_service._generate_report`, for each Meta source call
   `POST /api/meta/report-context` with the request's date range and the seller's `X-User-Id`.
   - Treat its `sources.<platform>` blocks as separate datasets, each with its own snapshot.
   - Never merge them into shipment rows.
   - Register `account_name` and `external_account_id` with the privacy context (`register_source_identities`).
5. **AI context:** pass these to `narrative_messages` alongside the computed shipment metrics:
   - `metrics`, `items`, `coverage`, `unavailable_metrics`, `data_stale`, `last_successful_sync_at`,
     `caveats` and `comparison_rules`.

   Keep the existing grounding: every claim must reference a provided metric. Totals exist only where
   `additive` is true.
6. **Status mapping:**

| `report-context` status | Report behavior (as implemented) |
|---|---|
| `not_connected` | 409 `meta_not_connected`, `details: {platforms, action: "connect"}` |
| `reconnect_required` | 409 `meta_reconnect_required`, `details: {platforms, action: "reconnect"}` |
| `stale` | Report generated; the account section says "may be out of date" with the last sync time |
| `no_data` | Every requested source empty (and no shipments): 422 `meta_no_data` with the reason (e.g. "the first sync has not finished yet"). Otherwise that source's section says so. No zeros are invented. |
| Ai_Box_Service rejects the BigShip login | 401 `upstream_auth_failed` |
| Meta not configured on Ai_Box_Service | 503 `meta_not_configured` |

Requests without `data_sources`, or with only `["shipments"]`, are unchanged.

### `POST /api/meta/report-context` response (abridged)
```json
{"requested_period": {"from_date": "2026-11-01", "to_date": "2026-11-16", "timezone": "Asia/Kolkata"},
 "sources": {
  "shipments": {"status": "external"},
  "whatsapp": {"status": "ok", "caveats": ["…"], "accounts": [{
    "account_name": "Seller WABA", "last_successful_sync_at": "…", "data_stale": false,
    "coverage": {"first_date": "2026-11-01", "last_date": "2026-11-16", "days_with_data": 16, "complete": true},
    "metrics": {"messages_sent": {"kind": "daily", "unit": "messages", "additive": true, "total": 1840,
                                  "daily": [{"date": "2026-11-01", "value": 120}]},
                "pricing_volume": {"kind": "daily", "additive": true,
                                   "by_dimension": [{"dimension": "MARKETING", "total": 400, "days_with_data": 16}]}},
    "items": {},
    "unavailable_metrics": [{"metric": "template_read", "reason": "permission_required", "detail": "…"}]}]},
  "instagram": {"status": "not_connected", "error": {"code": "META_NOT_CONNECTED", "action": "connect"}}},
 "comparison_rules": ["Report each metric under its own source and account; never add values from different sources together.", "…"]}
```

## Angular panel (`/ai_box/create`)

This is implemented in `International-User-Panel/src/app/modules/ai_box`. Every call goes to
`${environment.aiBoxApiUrl}/api/meta`, which is the gateway.

| File | What it does |
|---|---|
| `services/meta-integration.service.ts` | Loads readiness (`/platforms`, once) and `/connections`. Runs the connect flows, then sync (`POST …/sync`, polls `/sync-jobs/{id}` every 3 s) and disconnect (`DELETE`; history kept). |
| `components/data-sources/meta-connect-modal.*` | One row per platform. Connected accounts show the last-synced time, a "may be out of date" note when `data_stale`, and **Sync now** / **Disconnect** buttons. |
| `pages/ai-report/ai-report.component.ts` | On load, handles `?meta_status=…` from the OAuth callback |
| `models/data-source.model.ts` | `MetaConnection.accounts` (`MetaAccount`) |

**Connect flows:**
- **WhatsApp (Embedded Signup v4):** when the modal opens, the panel calls `POST /connect/whatsapp/start` and
  loads Meta's JS SDK (`connect.facebook.net`). This has to happen first so that **Connect** can call
  `FB.login(cb, login_options)` inside the click; browsers block sign-in windows opened later.
  - It listens for `WA_EMBEDDED_SIGNUP` messages, accepting only https `*.facebook.com` origins.
  - It then posts `code`, `waba_id`, `phone_number_id`, `business_id` and `event` to
    `/connect/whatsapp/complete`. `CANCEL` and `ERROR` are shown to the seller, and complete is never called.
- **Instagram / Facebook:**
  1. A popup opens inside the click and is pointed at `authorization_url`, which must be https
     `facebook.com` or `instagram.com`.
  2. Meta redirects to the backend callback, which sends the popup to `META_FRONTEND_RETURN_URL`
     (`https://<panel>/ai_box/create`) with `meta_status`, `meta_transaction` and `meta_platform`.
  3. That page, inside the popup, posts `{source: 'bigship-meta-connect', …}` to its opener, restricted to the
     same origin, and closes.
  4. The opener checks platform and transaction, then calls `/connect/{platform}/complete`.
  5. Without an opener, for example when the popup became a tab, the page completes the connection itself
     and removes the parameters from the URL.

**Status and errors:** the panel shows "Connected" only after `/connections` reports it. Backend
`detail.message` texts are shown as-is. A platform with `configured: false` shows "not enabled on the server
yet", which is the current state until the Meta app settings are filled in.

**Identity:** every `/api/meta` call carries the seller's BigShip login as `X-Shipment-Authorization`.
`AiBoxIdentityInterceptor` strips that header from any other host.

**Reports:**
- Connected WhatsApp, Instagram and Facebook sources can be selected on their own or with Shipments. The panel
  sends `data_sources` only when a Meta source is selected, so shipment-only requests are unchanged.
- Attachments can't be combined with Meta sources yet, and the panel says so.
- On `meta_not_connected` or `meta_reconnect_required`, the panel reloads the connection states.

## Tests

```bash
pytest                                   # whole suite; Meta DB tests skip without a test database
docker run -d --rm --name meta-test-pg -e POSTGRES_PASSWORD=metatest -e POSTGRES_DB=meta_test \
  -p 127.0.0.1:15439:5432 postgres:17-alpine
META_TEST_DATABASE_URL=postgresql://postgres:metatest@127.0.0.1:15439/meta_test pytest tests/meta_integration
```

The DB tests drop and recreate the `meta_*` tables, so **never** point `META_TEST_DATABASE_URL` at the shared
database. The root `tests/conftest.py` blanks `DATABASE_URL`, so ordinary test runs never touch it.

Coverage:
- Connection start, complete and callback
- OAuth state, single use, expiry and seller binding
- Code exchange
- WABA, phone number and Page grant validation
- Account eligibility
- Seller isolation on every endpoint
- Duplicate connections and reconnection
- Token expiry and refresh
- Disconnect, with and without data deletion
- Webhook verification, signatures, dedupe, attribution and minimization
- Initial, manual and incremental sync; idempotent upserts
- Rate-limit backoff, retry exhaustion, lease recovery
- Unsupported and empty metrics
- Normalization
- Report context: single source, multiple sources, not connected, expired, non-additive metrics
- Admin key rotation
- Deauthorize and data deletion
- No tokens in logs or responses
- Seller identity (`test_identity.py`):
  - the BigShip-verified seller wins over a spoofed `X-User-Id`;
  - a missing, malformed, expired or inactive login is rejected;
  - a BigShip outage is retryable;
  - token-hash caching;
  - header mode is refused in production;
  - per-seller `/platforms` readiness.
- Gateway (`AI_python_backend/tests/test_meta_reports.py`):
  - Meta-only and combined reports;
  - not connected, reconnect, no data, stale;
  - rejected login, not configured;
  - AI observations grounded in the supplied numbers;
  - unchanged requests without `data_sources`.

## Troubleshooting

| Symptom | Check |
|---|---|
| `Meta integration disabled: …` in the log | It names the missing setting or the database error |
| `platforms[].missing_configuration` not empty | Set the named variables, then restart |
| `META_TRANSACTION_INVALID` | The attempt is older than 10 minutes, was already used, or the callback never arrived. Check the registered redirect URI. |
| WhatsApp `META_PERMISSION_DENIED` | The seller didn't share that WABA in signup, or the phone number isn't in it |
| Facebook Page in `skipped` with `analyze_task_required` | The seller's Page role lacks Insights access |
| `template_*` shows `permission_required` | The seller hasn't enabled template insights in WhatsApp Manager |
| Webhook 401 | Wrong App Secret, or a proxy changed the body |
| Webhook events `unmatched` | The WABA or phone number isn't in a connected account |
| Jobs stay `queued` | Is a worker running (`META_WORKER_ENABLED` or the separate process)? Is the database reachable? |
| psycopg async error on Windows | The module uses psycopg's synchronous pool in threads, so this should not occur. Don't switch to the async pool on Windows. |
