# Catchment — CLAUDE.md

Personal content-intelligence pipeline. Ingests from WhatsApp, X bookmarks,
Substack RSS, and email (IMAP); extracts text/transcripts/OCR; classifies into
a *dynamic*, self-growing tag graph (no fixed taxonomy); stores in Postgres +
pgvector; surfaced via an Appsmith admin dashboard, a recommender, and a
gpt-researcher-based deep researcher.

## Stack (do not introduce alternatives without asking)
- Python 3.12, FastAPI (webhook/API surface), RQ + Redis (async jobs)
- Postgres + pgvector — single source of truth, no separate vector DB
- BGE-M3 (embeddings), PaddleOCR-VL (OCR), faster-whisper (transcription)
- Appsmith (admin UI) reads Postgres directly — do not build a custom frontend
- Langfuse (self-hosted) for LLM/agent tracing — every LLM call goes through it
- No n8n. Orchestration is plain Python (FastAPI + RQ), deliberately, so the
  bespoke classification logic stays testable and debuggable.

## Structure
```
catchment/
  ingestion/        # webhook + poller connectors, one file per source
  extraction/        # OCR, transcription, article parsing
  classification/    # embedding + dynamic tag assignment/creation
  storage/           # SQLAlchemy models, repository layer, Alembic migrations
  agents/             # deep researcher, recommender
  admin/              # Appsmith config/exports, not app code
  tests/
```

## Hard constraints — YOU MUST follow these
- **Never hardcode or print API keys, tokens, or DB credentials.** Read from
  env vars via `catchment/config.py` only. If you ever need a secret to test
  something, ask — do not fetch it yourself or write it into a file.
- **Never log full message bodies, transcripts, or email content at INFO
  level.** This pipeline ingests personal correspondence. Log IDs and
  metadata; redact content in logs.
- **Never commit `.env`, `*.pem`, or anything under `/secrets`.** If you
  create a new config file with real values, stop and ask before committing.
- **All ingestion jobs are keyed by `(source, source_id)`, enforced as a
  unique constraint at the DB level** — not just deduped in application code.
- **Recursive CTEs must have an explicit depth bound.** No unbounded graph
  walks over `tags`.
- **Taxonomy merges/splits are proposed, never auto-executed.** Write them to
  the review queue; a human approves before the merge runs.

## Conventions
- All DB writes go through the repository layer in `storage/repositories.py`
  — no raw queries scattered through connector/service code.
- Every new classifier decision path needs a test with a fixture in
  `tests/fixtures/`. Don't consider a task done until tests pass.
- New Alembic migrations only — never edit an already-applied migration.
- Type hints required on all public functions; this is a personal project but
  still gets `mypy --strict` in CI.

## Reference (not loaded automatically — read when relevant)
- `@docs/schema.md` — full ERD and column-level rationale
- `@docs/taxonomy.md` — how tag assignment/creation/merge logic works
