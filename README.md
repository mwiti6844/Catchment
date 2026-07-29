# Catchment

Personal content-intelligence pipeline. Ingests from WhatsApp, X bookmarks,
Substack RSS and email (IMAP); extracts text, transcripts and OCR; classifies
into a dynamic, self-growing tag graph; stores everything in Postgres +
pgvector. Surfaced through an Appsmith dashboard, a recommender, and a
gpt-researcher-based deep researcher.

## Setup

### Containers (recommended)

Brings up Postgres+pgvector, Redis, the API and an RQ worker. Migrations run to
completion before the app starts, so nothing ever talks to an un-migrated schema.

```bash
cp .env.example .env      # fill in; .env is gitignored
docker compose up -d
curl localhost:8000/health
```

One image serves both the API and the worker, differing only in `CMD`. Secrets
arrive through `env_file` at run time and are never baked into a layer.

If something already holds port 8000 (a local `php artisan serve` will), the
ports are parameterised:

```bash
API_PORT=8010 docker compose up -d
```

### Host Python

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

cp .env.example .env
createdb catchment
alembic upgrade head
```

Model runtimes are optional extras so the core package stays quick to install:

```bash
pip install -e '.[extraction]'   # PaddleOCR-VL, faster-whisper, trafilatura
pip install -e '.[embedding]'    # BGE-M3
```

## Running

```bash
uvicorn catchment.api:app --reload   # webhook + API surface
catchment-worker                     # RQ worker for the pipeline queue
```

Polled sources (Substack RSS, IMAP, X bookmarks) run as RQ jobs; only
webhook-driven sources touch the API.

### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness; reports version and environment |
| `GET` | `/webhook/whatsapp` | Meta's subscription handshake (echoes `hub.challenge`) |
| `POST` | `/webhook/whatsapp` | Inbound messages; HMAC-SHA256 verified |

The webhook does only what has to be synchronous — verify the signature,
upsert the item, enqueue a job — and returns. Meta retries anything it thinks
was slow or failed, so no extraction or model call happens in the request.

### Receiving real WhatsApp traffic (local development only)

For production use a stable HTTPS endpoint instead — see
[Production deployment](#production-deployment).

Meta cannot deliver to `localhost`, so a public HTTPS URL is required. A quick
tunnel is enough for development. Its hostname is **random and changes on every
restart**, so Meta delivery breaks whenever it stops — fine while iterating,
not something to leave running:

```bash
docker compose up -d
cloudflared tunnel --url http://localhost:8000     # or: ngrok http 8000
```

Register the resulting HTTPS URL + `/webhook/whatsapp` as the callback in the
Meta app dashboard, with `CATCHMENT_WHATSAPP_VERIFY_TOKEN` as the verify token.
Meta calls `GET` first for the handshake, then `POST`s messages signed with
`CATCHMENT_WHATSAPP_WEBHOOK_SECRET` (the app secret).

## What works end-to-end today

Slice one is the ingestion spine, proven with one real connector:

```
WhatsApp webhook ──verify HMAC──> items ──RQ job──> extractions ──> item_tags
```

Concretely: forward yourself a WhatsApp message and it becomes an `items` row,
an `extractions` row (via the passthrough extractor, since a text message
arrives as text), and an `unclassified` entry in the Appsmith review queue.

The **IMAP connector** is implemented too (`ingestion/email_imap.py`) but not
yet scheduled — it produces `RawRecord`s on demand and has no poller job behind
it. It over-fetches on purpose: the unique constraint absorbs repeats, so there
is no local UID watermark to drift out of sync.

### Classification

```
text ──BGE-M3──> embedding ──nearest items──> candidate tags ──LLM──> assigned tags
```

Embedding happens *before* the model is asked, so the prompt carries the tags
already in use on similar content. Skipping that is what makes a classifier
coin `ML Ops` next to an existing `MLOps`.

- **The LLM is routed, not hard-wired.** `CATCHMENT_LLM_PROVIDER` selects a
  registered provider (Groq today); adding one is a registration, not a change
  to the classifier. Langfuse tracing wraps the provider, so "every LLM call is
  traced" survives a provider swap instead of being something each new provider
  must remember.
- **BGE-M3 runs in its own container** (`embedder/`), because FlagEmbedding
  pulls ~3GB of torch the API and worker never use.
- **Classification degrades rather than failing.** An embedder outage, a rate
  limit, or an unparseable response falls back to the `unclassified` tag — the
  item stays visible in the review queue instead of being stranded.

Deliberately still placeholders — these are *not* the real implementations:

- **Passthrough extractor** copies source-supplied text straight through. Real
  OCR (PaddleOCR-VL) and transcription (faster-whisper) are a later slice, so
  media items currently produce an extraction only if they carry a caption.
- **`unclassified` is now the fallback**, not the default path. It is marked
  `origin='import'` rather than `'llm'`, so a degraded classification is
  distinguishable from a model decision when reviewing the graph.

## Production deployment

A DuckDNS subdomain, a VPS, and Caddy terminating TLS with automatic Let's
Encrypt certificates. Full runbook: **[deploy/README.md](deploy/README.md)**.

```bash
sudo ./deploy/provision.sh          # once, on a fresh Ubuntu VPS
docker compose --env-file .env --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml up -d
```

The overlay leaves the base `docker-compose.yml` untouched, so local
development is unaffected. In production **only Caddy publishes ports** — the
base file otherwise exposes Postgres, Redis, the embedder and Langfuse to the
host, and `ufw` does not filter Docker-published ports. Always pass both files.

## Tracing (Langfuse)

Self-hosted, Postgres-only (`langfuse/langfuse:2` — v3 additionally needs
ClickHouse, MinIO and its own Redis). The org, project, API keys and UI login
are provisioned **headlessly on first boot** from `.env`, so `docker compose up`
yields a working tracing stack rather than a UI you have to click through.

```bash
# generate a key pair, put both in .env, then bring the stack up
python -c "import uuid;print(f'pk-lf-{uuid.uuid4()}');print(f'sk-lf-{uuid.uuid4()}')"
docker compose up -d          # UI at http://localhost:3000
```

`docker-compose.yml` feeds the *same* `.env` values to the server as
`LANGFUSE_INIT_PROJECT_*`, so the app's keys and the server's cannot drift.
That drift is worth guarding against: ingestion is fire-and-forget, so a
mismatch returns 401 on a background thread and every trace is dropped while
calls still look successful. Two things make it visible now — `auth_check()`
runs once at client construction and logs an error naming the host, and the SDK
is pinned to `<3` to match the v2 server (the v3+ SDK posts OTLP to an endpoint
a v2 server does not expose, and `auth_check()` passes anyway).

**Langfuse Cloud keys will not work against the self-hosted instance.** That is
the most likely cause of an auth error at startup.

## Checks

```bash
pre-commit install                       # runs the checks below on every commit

pytest                                   # unit tests, no database required
mypy catchment tools                     # strict
ruff check catchment tools
python tools/check_log_fstrings.py catchment   # content-in-log-message lint

# Integration tests against a throwaway database
createdb catchment_test
CATCHMENT_TEST_DATABASE_URL=postgresql+psycopg://localhost/catchment_test \
    pytest -m integration
```

The default suite runs without Postgres. The integration suite is what proves
the database-level guarantees — it is skipped, not silently passed, when no
test database is configured. **CI sets `CATCHMENT_REQUIRE_INTEGRATION=1`**,
which turns a missing test database from a skip into a failure, so those
guarantees can never quietly stop being checked.

## Layout

```
catchment/
  ingestion/       # one connector per source; contract in base.py
  extraction/      # OCR, transcription, article parsing
  classification/  # embedding + dynamic tag assignment/creation
  storage/         # models, repositories, Alembic migrations
  agents/          # deep researcher, recommender
  admin/           # Appsmith exports — not app code
  tests/
docs/
  schema.md        # ERD and column-level rationale
  taxonomy.md      # tag assignment/creation/merge logic
```

## Invariants worth knowing before changing anything

These are enforced in the schema and covered by tests, not left to convention.

- **Ingestion is keyed by `(source, source_id)`** under a unique constraint.
  Connectors may over-fetch; re-ingestion is a no-op.
- **All database writes go through `storage/repositories.py`.** No raw queries
  in connector or service code.
- **Recursive walks over `tags` are depth-bounded** in the recursive term of
  the CTE. The graph is not guaranteed acyclic.
- **Taxonomy merges and splits are proposed, never auto-executed.** A merge
  reaches `applied` only via a recorded human approval; the database rejects
  any other path.
- **Secrets come from `catchment/config.py` only**, wrapped in `SecretStr`.
- **Personal content never reaches INFO logs.** A `RedactionFilter` on the root
  handler enforces this — log ids and counts, and use `content_summary()` when
  you need to say something about a body. Two gaps the filter cannot close:
  content interpolated into a message *string* (caught by
  `tools/check_log_fstrings.py`), and third-party libraries logging our
  payloads. RQ does exactly that, which is why `jobs/queue.py` sets an explicit
  `description` — without it, `rq.worker` prints the message body at INFO.

See `CLAUDE.md` for the full working agreement.
