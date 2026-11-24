# Email Integration ("Send to" Gmail / Outlook)

On a report, **Send to ▸ Gmail** (or **Outlook**) opens a new browser tab that shows a ready **draft** in the
user's own mailbox:
- subject;
- a short AI-written email (greeting, 2-3 lines answering the user's report request, up to 3 bullets with the
  main numbers, closing, sign-off), not a copy of the report;
- the report PDF attached.

The user adds recipients and sends it from Gmail/Outlook. **AI Box never sends email itself.**

The first time, the same tab shows the Google/Microsoft sign-in. After that, a click goes straight to the draft.

> **Status:** code complete and tested against fake Google, Microsoft and BigShip APIs plus a real PostgreSQL.
> To try it with a real account, set up the OAuth apps described in [Setup](#setup).

## Flow

```
Angular report page: click "Send to ▸ Gmail"
  1. window.open() a new tab ("Preparing your Gmail draft…"), inside the click
  2. GET /api/email/providers            ── is a Gmail mailbox connected?
       no ▸ POST /api/email/connect/gmail/start ▸ the tab goes to Google sign-in
            Google ▸ GET /api/email/oauth/callback (stores the code) ▸ "Signed in…" page
            the report page polls /connect/transactions/{id}, then POST /connect/gmail/complete
  3. POST /api/reports/{id}/email/compose        (AI_python_backend)
       AI writes the email (falls back to the report's own text if the AI fails or invents a number)
       ▸ plain email HTML (escaped) + PDF export
       ▸ POST /api/email/internal/draft          (Ai_Box_Service, server-to-server only)
            seller from BigShip ▸ mailbox ownership ▸ hourly limit ▸ token refresh ▸ create draft
  4. the tab is pointed at open_url ▸ Gmail compose window with the draft
```

- **Expired access or too little permission:** the mailbox is marked `reconnect_required`, and the page signs
  in again once, in the same tab.
- **Browser cut the link to the tab:** some sign-in pages do this. The page then shows a toast:
  "Your draft is ready. Click here to open it in Gmail."

## Who does what

| Part | Its job | Files |
|---|---|---|
| Angular `ai_box` | "Send to" menu, new tab, sign-in polling | `pages/ai-report/ai-report.component.ts` (`sendReportTo`), `services/email-integration.service.ts`, `models/email.models.ts` |
| AI_python_backend | AI email text, HTML, PDF | `app/api/report_email_routes.py` (`/email/compose`), `app/services/report_email.py`, `app/services/ai_box_email.py`, gateway route `/api/email` in `app/api_gateway.py` (blocks `/internal`) |
| Ai_Box_Service `email_integration` | OAuth, encrypted tokens, drafts in Gmail/Outlook | see the next table |

### Ai_Box_Service `app/modules/email_integration/`

| File | Responsibility |
|---|---|
| `module.py` | Startup and shutdown. If not configured, logs why and leaves `app.state.email = None`. |
| `config.py` | `EMAIL_*` settings and per-provider readiness. |
| `errors.py` | Error codes, returned as `{"detail": {code, message, retryable, action}}`. |
| `context.py` | Shared runtime: settings, repository, cipher, providers, seller identity, access-token cache. |
| `repository.py` | Every SQL statement. Each query is filtered by `seller_id`. |
| `migrations/0001_*.sql` | `email_connections`, `email_credentials`, `email_oauth_transactions`, `email_draft_log`, `email_audit_logs`. |
| `providers/base.py` | Provider interface. Maps errors to `invalid_grant` / `permission` / `rate_limited` / `rejected` / `unavailable`. |
| `providers/gmail.py` | `drafts.create` with raw MIME (up to 35 MB). Link: `mail.google.com/mail/?authuser=<email>#drafts?compose=<id>`. |
| `providers/outlook.py` | `POST /me/messages`, then the attachment (an upload session above 3 MB). The link is Graph's `webLink`. |
| `mime.py` | The Gmail message: text + HTML + PDF, with no header injection. |
| `services/connections.py` | Sign-in flow, disconnect, access-token refresh. |
| `services/drafts.py` | Validate, check ownership, limit, create the draft, log it. |
| `router.py`, `schemas.py` | HTTP API. |

Shared helpers in `app/core/`:

| File | Responsibility |
|---|---|
| `crypto.py` | AES-256-GCM token cipher. |
| `pg.py` | Postgres pool and migrations. |
| `seller_identity.py` | Finds the seller by asking BigShip who owns the login token. |

## Security

| Rule | How |
|---|---|
| Seller identity | Verified with BigShip (`EMAIL_SELLER_IDENTITY=bigship`). `header` is for local development only. |
| Who can create drafts | Only AI_python_backend: service token + `X-Email-Internal-Key` + the gateway blocks `/api/email/internal/*`. |
| Sign-in | OAuth code + PKCE. Only `sha256(state)` is stored. The code is exchanged only by the seller who started the sign-in. |
| Tokens | Refresh tokens are encrypted (AES-256-GCM, AAD = connection ID). Access tokens stay in memory. Disconnecting revokes the token at Google and removes it from the database. |
| AI | Sees only the report's aggregates. PrivacyGuard runs. Links and markup are removed. A number not in the report means the report's own text is used instead. |
| HTML | A plain email (paragraphs, bullets, sign-off). Every value is escaped. Numbers are copied from the report only. |
| Logs | `email_draft_log` keeps who, which mailbox, subject, PDF name, size and sha256, status. It never keeps the body. |
| Limits | 10 MB PDF, PDF only, 60 drafts per seller per hour. |

## Permissions and what they cost

| Provider | Scopes | Note |
|---|---|---|
| Gmail | `openid email https://www.googleapis.com/auth/gmail.compose` | `gmail.compose` is a **restricted** scope. It works for test users while the app is in *Testing* (tokens expire after 7 days there). Publishing to all users needs Google verification **and** a third-party security assessment (CASA), which is yearly and takes weeks. |
| Outlook | `openid email offline_access User.Read Mail.ReadWrite` | Graph needs `Mail.ReadWrite` to create a draft. Some company tenants require admin consent. |

## Setup

### Ai_Box_Service `.env`

```env
# generate a key: .venv\Scripts\python.exe -c "from app.core.crypto import new_key; print(new_key())"
EMAIL_TOKEN_ENCRYPTION_KEYS={"1":"<generated key>"}
EMAIL_TOKEN_ENCRYPTION_ACTIVE_KEY=1
EMAIL_INTERNAL_KEY=<long random string, also in AI_python_backend>
EMAIL_OAUTH_REDIRECT_URI=http://127.0.0.1:8200/api/email/oauth/callback
EMAIL_GOOGLE_CLIENT_ID=...
EMAIL_GOOGLE_CLIENT_SECRET=...
EMAIL_MICROSOFT_CLIENT_ID=...
EMAIL_MICROSOFT_CLIENT_SECRET=...
```

### AI_python_backend `.env`

```env
AI_BOX_EMAIL_INTERNAL_KEY=<same as EMAIL_INTERNAL_KEY>
```

### Google (Google Cloud console, Google Auth Platform)

1. Enable the **Gmail API**.
2. In **Data Access**, add these scopes: `openid`, `.../auth/userinfo.email` and `https://www.googleapis.com/auth/gmail.compose`.
3. In **Audience**, add the test users.
4. In **Clients**, create a **Web application** client with the redirect URI above.

### Microsoft (Entra admin center, App registrations)

1. Choose *any organizational directory + personal accounts*.
2. Add a Web redirect URI (the one above).
3. Add delegated Graph permissions: `openid`, `email`, `offline_access`, `User.Read`, `Mail.ReadWrite`.
4. Create a client secret.

### Restart both services

The Ai_Box_Service log should show:

```
module.email_integration | started (providers ready: [...])
```

In production, the redirect URI is the public **https** gateway URL in all three places: Google, Microsoft and
`.env`.

## Tests

```bash
# Ai_Box_Service: needs a throwaway Postgres (never a shared one)
docker run -d --rm --name email-test-pg -e POSTGRES_PASSWORD=emailtest -e POSTGRES_DB=email_test \
           -p 127.0.0.1:15440:5432 postgres:17-alpine
set EMAIL_TEST_DATABASE_URL=postgresql://postgres:emailtest@127.0.0.1:15440/email_test
.venv\Scripts\python.exe -m pytest tests/email_integration

# AI_python_backend
.venv\Scripts\python.exe -m pytest tests/test_report_email.py tests/test_api_gateway.py

# Angular
npx ng test --watch=false --include=src/app/modules/ai_box/services/email-integration.service.spec.ts
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| Toast: *Email is not set up on this server yet* | Ai_Box_Service log shows `Email integration disabled: …`. Set the keys and restart. |
| Toast: *Gmail is not set up on this server yet* | Google client ID/secret or `EMAIL_OAUTH_REDIRECT_URI` is missing. |
| Google: `redirect_uri_mismatch` | The URI in Google Cloud must equal `EMAIL_OAUTH_REDIRECT_URI` exactly. |
| Google: *Access blocked / app not verified* | The account is not in **Audience → Test users**. |
| Toast: *did not grant permission to create drafts* | The user unticked the Gmail permission, or `gmail.compose` is missing in **Data Access**. Click again and allow it. |
| Toast: *Email is not configured* when composing | `AI_BOX_EMAIL_INTERNAL_KEY` ≠ `EMAIL_INTERNAL_KEY`. |
| The new tab does not open | The browser blocked pop-ups for the panel. Allow pop-ups for the site. |
