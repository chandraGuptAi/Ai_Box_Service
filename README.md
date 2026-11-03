# Ai Box Service

Modular FastAPI backend. Every feature is a self-contained **module** that can be added or removed without touching the rest of the code.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env
python -m app.main              # or: uvicorn app.main:app --reload --port 8100
```

Docs: http://localhost:8100/docs  ·  Health: http://localhost:8100/api/health

Browser requests from `http://localhost:4200` are allowed by default. Set
`CORS_ALLOWED_ORIGINS='["http://localhost:4200","https://your-panel.example"]'`
in `.env` to change the allowed origins, then restart the service. CORS preflight
is handled before service-token authentication; actual requests still require
the configured authentication headers. When using a gateway on port 8000, that
gateway must handle preflight too and map its public path to this service's
`/api/audit_log/*` routes.

## Docker (Linux)

The image uses Python 3.11, CPU PyTorch, the required spaCy models and local
Tesseract OCR (English/Hindi). Compose targets `linux/amd64` to match the pinned
PyTorch CPU wheel. It runs as a non-root user with one Uvicorn worker and no reload.

1. Create `.env` from `.env.docker.example` on the server if you do not already
   have one. Keep an existing `.env` and update its values instead of overwriting it.
2. Set a non-empty random `SERVICE_TOKEN` matching the gateway's
   `AI_BOX_SERVICE_TOKEN`. Select the correct `BIGSHIP_API_BASE_URL`; the example
   uses staging. Configure vision credentials only if needed.
3. Build and start:

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 -f ai-box-service
curl --fail http://127.0.0.1:8100/api/health
```

Compose passes `.env` into the container, including settings read directly from
the process environment. It overrides debug mode, API prefix and storage paths
for the container. `.env`, local data and the Windows virtualenv are excluded
from the build context. Build requires internet access for dependencies/models.

The named `ai-box-data` volume persists uploads, SQLite report memory and the
Hugging Face cache across container rebuilds. Existing files in the host `data/`
directory are not imported automatically. Back up this volume; `docker compose
down` keeps it, while `docker compose down -v` deletes it.

The semantic model downloads on first use and is then cached. Set
`REPORT_MEMORY_SEMANTIC=false` for spelling-only matching without that download.
Use `ATTACHMENT_OCR_LANGUAGE=eng+hin` for bilingual OCR.

The published port defaults to `127.0.0.1:8100`. A gateway on the same host can
use `http://127.0.0.1:8100`; a gateway in the same Docker network can use
`http://ai-box-service:8100` after connecting both services to that network.
For a gateway on another server, set `BIND_HOST` to the host's private IP and
allow TCP 8100 only from that gateway. For a local vision server, container
loopback refers to the container itself; the current vision allowlist supports
loopback endpoints or the official DeepSeek origin.

After source changes, rebuild with `docker compose up -d --build`. Restart after
environment-only changes with `docker compose up -d --force-recreate`.

## Structure

```
app/
  core/                  # framework only - no business logic
    config.py            # settings from .env
    module_base.py       # BaseModule every module extends
    module_loader.py     # reads modules.yaml, loads & mounts modules
  modules/
    _template/           # copied by scripts/new_module.py
    health/              # example module
  main.py                # create_app()
modules.yaml             # which modules are enabled + their config
scripts/new_module.py    # module generator
```

## Modules

| Task    | How |
|---------|-----|
| Add     | `python scripts/new_module.py my_feature` → creates `app/modules/my_feature/` and enables it |
| Disable | set `enabled: false` in `modules.yaml` |
| Remove  | delete the entry in `modules.yaml` and the folder `app/modules/<name>/` |

A module folder contains:

- `module.py` – `class Module(BaseModule)`: name, `depends_on`, `get_router()`, `on_startup()`, `on_shutdown()`
- `router.py` – HTTP endpoints, mounted at `/api/<name>`
- `service.py` – business logic
- `schemas.py` – request/response models

Rules that keep modules removable:

1. A module imports only from `app.core` and itself — never from another module's files.
2. If it needs another module, list it in `depends_on` and reach it via `app.state.modules.get("<name>")`.
3. Per-module settings go under its entry in `modules.yaml` and are read from `self.config`.

## Tests

```bash
pytest
```
## Attachment processing

The `attachments` module exposes internal APIs under `/api/attachments`. Uploads
are scoped to `X-User-Id`, which the authenticated main backend must supply.
Use `X-Ai-Box-Service-Token` matching `SERVICE_TOKEN` for internal authentication.
These routes do not require or forward a Bigship credential.

Supported input: PDF, DOCX, XLSX, PPTX, PNG, JPEG, WebP, TIFF, GIF, BMP. Actual file
signatures are checked instead of trusting filenames or browser MIME headers.
Legacy DOC/XLS/PPT, macro-enabled/embedded executable Office content and encrypted
documents are rejected with an explicit response. Upload limit defaults to 20 MiB;
extraction has separate page, cell, pixel and expanded-archive limits.

Internal workflow:

1. `POST /api/attachments` with multipart field `file` returns an opaque attachment ID.
2. `GET /api/attachments/{id}` returns extracted text, full bounded spreadsheet rows,
   formula/cache metadata, and layout/style metadata. This is private, unsanitized
   source material; run it through the main backend's privacy boundary before AI dispatch.
3. `POST /api/attachments/planning-context` accepts `prompt` and `attachment_ids`.
   It returns bounded context, source-selection instructions and a JSON decision schema.
   Spreadsheet samples and text truncation are explicitly marked; use the full inspection
   for calculations. The source-selection skill is loaded from `.agents/skills/attachment-report/SKILL.md`
   at runtime. The context endpoint does not run AI or calculate report values.
4. The main backend asks its AI to understand the prompt and select attachment roles
   and `attachment_only`, `api_only`, `combined`, or `clarification_required`.
   For unread visual files this role decision happens first; only selected visual attachments
   go to `POST /api/attachments/prepare` and the configured vision model. Unused images are
   never dispatched. Context is then refreshed for the full typed analysis plan.
5. `POST /api/attachments/validate-plan` accepts the same prompt/IDs plus `decision`.
   It checks ownership, classification and source consistency. Attachment-only plans
   return `fetch_external_data=false` and `fetch_preview_count=false`. UI selections
   have no effect here; the AI must explicitly choose whether selected filters apply.
   Unreadable data attachments block generation. None of these endpoints call Bigship.
6. `POST /api/attachments/validate-report-plan` validates the complete typed plan.
   `POST /api/attachments/execute` calculates grouped counts, sums, averages, extrema and distinct
   counts in Python over the complete selected table range. Filters, sorting, chart selection and
   bounded output rows are supported. Columns accept numeric indexes or unambiguous header names.
   Exact document excerpts resolve against original source text, including quote candidate indexes.
   `POST /api/attachments/{id}/render` applies
   approved text replacements or explicit worksheet/cell edits to a copy of the original.
   It returns DOCX/XLSX/PPTX, preserving unrelated OOXML parts, styles, charts and images.
   Original uploads are never overwritten.

Example decision:

```json
{
  "prompt": "Excel ke data se report banao",
  "attachment_ids": ["<upload-id>"],
  "decision": {
    "source": "attachment_only",
    "attachments": [{"attachment_id": "<upload-id>", "role": "data"}],
    "use_selected_filters": false,
    "reason": "The user explicitly requests attachment data."
  }
}
```

Word/PPTX edits use `{"replacements":{"{{total}}":"3"}}`; matches spanning text
runs retain the first matching run's styling. Missing placeholders return 422.
Excel edits use `{"cells":[{"sheet":"Orders","cell":"B2","value":3600}]}`.
Existing template rows/cells are required. String values are written as literal text,
never executable formulas. Existing formulas remain, their stale caches are cleared,
and Excel is instructed to recalculate on open. Python does not evaluate formulas or
refresh cached chart images. Long replacement text may require a layout review.

`GET /api/attachments/{id}/preview/{zero_based_index}` provides normalized PNGs for
PDF pages and image frames. `GET /api/attachments/{id}/images/{index}` provides embedded
Office images in PNG form. These resources allow the main backend to supply images to
a configured vision model. DeepSeek `deepseek-flash` is supported through
`ATTACHMENT_VISION_BASE_URL=https://api.deepseek.com`, `ATTACHMENT_VISION_MODEL=deepseek-flash`
and `ATTACHMENT_VISION_API_KEY`. Local OpenAI-compatible vision endpoints are also supported.
Only the official DeepSeek origin or loopback endpoints are accepted. Service/Bigship credentials
are never forwarded to the model; the vision credential is separate. Image previews are sent to
the configured vision provider, while textual report planning goes through the main privacy boundary.
Uncertain image extraction stops processing. Prepared visual content is cached with the attachment.
Local OCR runs only if Tesseract and the configured language packs are installed;
otherwise responses explicitly mark missing OCR. OCR is not full chart/image understanding.
Office page/slide rendering, automatic legacy conversion, PDF design recreation and
arbitrary report-to-template layout mapping are not implemented yet. Exact visual
fidelity is not claimed by the template edit endpoint; render/review exports downstream.

Uploads expire after 24 hours by default; expired files are removed on startup and
subsequent uploads (at most one cleanup scan per minute). `DELETE /api/attachments/{id}`
removes a caller's upload immediately. Configure `ATTACHMENT_STORAGE_DIR` for persistence.

Install dependencies with `.venv\Scripts\python.exe -m pip install -r requirements.txt`.
The Angular AI Box page now uploads attachments through authenticated `AI_python_backend` routes.
`POST /api/reports/generate` accepts `source=attachment` and `attachment_ids`; the main backend
loads context, calls its AI planner, validates the plan, executes attachment operations, and fetches
external records only if selected by that validated plan. It returns signed report JSON and supports
PDF, DOCX, XLSX and PPTX exports. Generated template bindings use actual computed result cells;
the owner can download a filled original template through the report's template endpoint.


## Privacy filter

`POST /api/privacy/filter` masks personal data in messages that `AI_python_backend` is about to send
to an AI model, and returns `[REDACTED]` text plus `removed: [{category, ref}]`. It never rejects
content: Hindi, Hinglish and English questions pass, and only the personal parts are masked (known
seller identities, phone/email/PAN/GST/IFSC/UPI patterns, labelled names such as `mera naam ...`,
honorific and surname cues, and names found by the local spaCy models). `ref` is a fingerprint used
to count a value once; values are never returned or logged.

The engine (`app/modules/privacy/engine.py`) is kept identical to
`AI_python_backend/app/security/privacy_engine.py`, which runs locally when this service is down.
Startup fails if the spaCy models are not installed (`pip install -r requirements.txt`).

## Shipments

`POST /api/shipments/fetch` (Bigship token in `Authorization` or `X-Shipment-Authorization`) returns every
shipment matching `status_type`, `from_date`, `to_date` (IST, end exclusive) and `date_filter_type` from the
Bigship order-list API: total read first, concurrent pages, date windows for large ranges, page splitting on
5xx/timeouts; nothing is truncated. Before rows leave this service every name, contact detail, address,
identity/tax document and file link is removed (consignor/consignee keep city, state, pin, country);
`removed_fields` lists each removed field with its reason and count, never values. The lane (`N1-E2`) is
split into `origin_zone` and `destination_zone`. Cleaned results are cached per token and query for
`cache_ttl_seconds` (default 600; `fresh: true` skips it). Errors return `detail.code` with the same codes
as AI_python_backend (`source_too_large`, `upstream_auth_failed`, ...).

## Report memory

`/api/report_memory/*` remembers questions that produced reports (privacy-filtered by the caller), with
their plan and a score. `lookup` returns the plan of the same question (same scope shape, no explicit
dates, positive score) so the backend skips the AI planning call, plus up to 3 similar successful
questions as planner examples. `record` stores the outcome; `feedback` applies saved / exported /
shared / rerun / up / down once per report. Data: `data/report_memory.sqlite`.

Similarity = local multilingual model `paraphrase-multilingual-MiniLM-L12-v2` (meaning, Hindi <-> English)
combined with character n-grams (spelling, Hinglish). Measured: Hindi <-> English 0.6-0.8, Hinglish
0.45-0.5, unrelated < 0.2; `min_similarity` 0.45. About 12 ms per question, at most `threads` CPU
threads, ~450 MB RAM once loaded (first lookup). If the model cannot load, spelling similarity is used.

Offline server: download the model once where internet is available and copy the Hugging Face cache:
`python -c "from sentence_transformers import SentenceTransformer as S; S('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"`
then copy `~/.cache/huggingface` to the server and set `HF_HUB_OFFLINE=1`.
`REPORT_MEMORY_SEMANTIC=false` disables the model (spelling similarity only).

## Audit log (AI calling)

`/api/audit_log/*` reads the Voice AI admin API (`VOICEAI_BASE_URL`, default `https://voiceai.bigship.direct`) with
`VOICEAI_ADMIN_KEY` from `.env`; the admin key is added here and never returned. Call logs hold every client's
customer names and phone numbers, so each request needs `X-Audit-Key` equal to `AUDIT_LOG_API_KEY`
(empty = API disabled, 503). Through the gateway the same paths are `AI_python_backend/api/audit_log/*`.

| Endpoint | Voice AI |
|---|---|
| `GET /api/audit_log/call-logs` | `/v1/admin/call-logs`: `total`, `limit`, `offset`, `summary` (calls, answered, talk time, cost), `calls` |
| `GET /api/audit_log/call-logs/{call_id}` | `/v1/admin/call-logs/{call_id}` |
| `GET /api/audit_log/call-logs/{call_id}/recording` | `/v1/admin/call-logs/{call_id}/recording` (streamed; `?download=true` to save) |
| `GET /api/audit_log/call-analyses` | `/v1/admin/call-analyses` |

Filters (all optional): `client_id`, `type` (default `all`), `campaign_id`, `status` (call logs), `outcome`
(analyses), `phone`, `date_from`, `date_to`, `limit` (1-500, default 50), `offset`. Each call's `recording.url`,
`play_url` and `download_url` point to the recording endpoint above. A browser `<audio src>` cannot send
`X-Audit-Key`, so fetch the recording with the header and play the blob.


### Call-log CSV export

`GET /api/audit_log/call-logs/export/csv` downloads `call-logs.csv`.
Send the usual `X-Audit-Key` header and gateway service authentication.
With no query filters it exports every call; optional `client_id`, `type`,
`campaign_id`, `status`, `phone`, `date_from`, and `date_to` select matching calls.
For example: `/api/audit_log/call-logs/export/csv?status=completed&date_from=2026-11-01`.
The export fetches all matching pages; table `limit` and `offset` are ignored.
Nested analysis/recording values are JSON cells. UTF-8 BOM supports Excel, and
formula-like text is prefixed with an apostrophe for safe spreadsheet opening.
