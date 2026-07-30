# Roadmap

Everything not yet built, sequenced by dependency rather than by appeal.

**Status (2026-07-30):** Phases 0, 1, 2.1 and the Substack half of Phase 4 are
built and committed. What remains is listed below with those sections marked
DONE; the rest stands as written.

State this is written against: the ingestion spine is proven end-to-end
(WhatsApp → item → extraction → embedding → classification → tags), traced in
Langfuse, with a coded dashboard on a loopback-only API. What follows is
everything else.

The ordering principle throughout: **finish what is half-built before starting
what is unbuilt.** That is what Phases 0–2.1 were: two shipped features that
were inert for want of a small piece of upstream work.

---

## Phase 0 — Make what already exists actually work — DONE

Two gaps where the code was written and tested but had no data to act on.
Both are now closed. Retained here because the reasoning still explains why
the code looks the way it does.

### 0.1 Tag edge creation — DONE

**The problem it solved.** `TagRepository.add_edge` had no callers. The graph
held `tags=3, tag_edges=0`, so graph expansion in search reached nothing and
retrieval was behaving as vector-only.

**What shipped.** The classifier returns `broader_than` alongside each
suggestion; `classification/service.py` creates the edge. See
`docs/taxonomy.md` for the containment rules — validated parent, per-item cap,
cycle refusal.

### 0.2 Merge executor — DONE (splits deliberately not executed)

**The problem it solved.** `mark_applied` had no callers, so the review queue
was write-only: a human could approve a merge and nothing would ever act on it.

**What shipped.** `catchment/taxonomy/apply.py`, one savepoint per proposal,
idempotent. Approving in the dashboard executes the merge in the same
transaction that records the decision. Splits raise rather than approximate —
the payload names the new tags but not which items go where, and guessing would
be a taxonomy decision made by code.

---

## Phase 1 — The media pipeline — DONE

**This was the real prerequisite for all extraction, and it was not obvious.**

`raw_ref` was documented as "pointer to blob storage" while no blob storage
existed. For WhatsApp media it held a *Meta media id* — a pointer into their
API that no extractor could open — while `Extractor.extract(raw_ref)` promised
"the blob at raw_ref". Every item so far had been text, so nothing surfaced it.
All of Phase 2 was blocked behind this and it was on nobody's list.

`raw_ref` now means one thing. Source media ids live in `meta`.

### 1.1 Blob storage — DONE

`catchment/storage/blobs.py`: a `BlobStore` protocol with a filesystem
implementation, so S3/MinIO later is one implementation rather than a rewrite.
Refs are `blob://<key>` and carry no absolute path — they outlive this backend.
Writes are atomic; traversing keys are refused rather than normalised. Media
rides on a named volume, is gitignored outside Docker, and **needs backing up
alongside `pgdata`**.

### 1.2 WhatsApp media fetch — DONE (needs a token to run)

`catchment/ingestion/media.py` resolves a media id through the Graph API and
downloads the bytes. It runs inside the pipeline job, not the webhook — the
webhook must answer Meta quickly, and the pipeline already owns a transaction
and retry semantics. It degrades like classification does, and is idempotent
by the `raw_ref` check.

**Still blocked on you.** The code is built and tested, but downloading
anything needs `CATCHMENT_WHATSAPP_ACCESS_TOKEN`, which was deliberately
deleted from `.env` on the grounds that "inbound webhook operation does not
require it". That was correct then and is wrong now. Until it is set, media
items arrive and are tagged from their captions, and the skipped fetch is
recorded in `pipeline_failures` — nothing is lost except the bytes.

A short-lived user token expires in ~24h. For anything ongoing, issue a System
User token in Meta Business Settings with `whatsapp_business_messaging` on the
WABA; those do not expire on a timer.

**Risk that remains.** Media URLs from Meta are short-lived, and the media id
itself expires after a fixed window. If the queue backs up badly, a fetch can
find the id already gone — that surfaces as `MediaNotAvailable` in
`pipeline_failures` and is not retryable.

---

## Phase 2 — Real extraction

Replaces the passthrough extractor. 2.2 and 2.3 depend on Phase 1, which is
now in place.

### 2.1 Article parsing — DONE

`catchment/extraction/article.py`, over `url`, for link and article items. No
blob needed — it fetches the URL directly. The pipeline prefers article text
over the source's own, so a forwarded link no longer classifies on "worth
reading". A dead or paywalled link degrades to the caption.

### 2.2 OCR — PaddleOCR-VL

Images and stickers. Follows the embedder's pattern exactly: its own container,
its own image, an HTTP contract, so the worker image stays free of another
multi-GB dependency.

### 2.3 Transcription — Whisper

Voice notes, the highest-value WhatsApp content type.

**Decision to make.** `CLAUDE.md` specifies faster-whisper (local). Groq hosts
`whisper-large-v3-turbo` at **$0.04/hour of audio**. Given the LLM is already
hosted, another local multi-GB container may be the wrong trade — but
`CLAUDE.md` says not to substitute stack components without asking, so this is
an explicit decision, not a default.

**Done when.** A forwarded voice note becomes an `extractions` row and is
classified like any other item, end to end.

---

## Phase 3 — Dashboard v2

Deferred from v1 on purpose. Depended on Phase 0.1 — both features need a graph
that has edges. It can have them now, though the graph will stay empty until
enough items flow through the classifier to produce real placements.

### 3.1 Tag graph explorer

The thing wanted from the beginning: watching the taxonomy take shape and
drift, rather than reading a flat table. An explorable node/edge view seeded on
a tag, expanding within the depth bound, showing item counts and merge
candidates.

### 3.2 Insights

Trend detection and signal-vs-noise. What dominated the feed this week, which
tags are accelerating, a light version of "this is becoming a project for you".

**Risk worth naming early.** This is the first feature whose output is a
*claim about you* rather than a record of what happened. It needs to be
obviously derived — every insight traceable to the items behind it — or it
becomes an unfalsifiable horoscope.

---

## Phase 4 — Remaining connectors

Each follows the established `Connector` protocol, records connector health,
and over-fetches freely because the unique constraint absorbs repeats.

| Source | Notes |
| --- | --- |
| **Substack RSS** | **DONE.** Any RSS or Atom feed, via `CATCHMENT_SUBSTACK_FEEDS` and `catchment-poll-substack`. Never run against a real feed yet. |
| **IMAP** | Already built and unit-tested, never run against a real mailbox. This is a verification task, not a build. |
| **X bookmarks** | Hardest: OAuth 2.0 with refresh, and API access tiers change. Do last, and check current API terms before starting. |

---

## Phase 5 — Agents

The original brief's endpoints, and the least specified. Both depend on the
retrieval layer built in the dashboard slice, which is why they come after
Phase 0.1 makes it fully functional.

- **Recommender** — "what should I read next", over embeddings and the tag graph.
- **Deep researcher** — gpt-researcher based, using the corpus as its source.

Both route through the existing LLM router, so tracing and provider-swapping
come free.

---

## Phase 6 — Production

Designed and committed; not running.

1. **Deploy** — VPS with static IPv4, DuckDNS, `provision.sh`, both compose
   files. `deploy/README.md` is the runbook.
2. **Backups** — highest-priority ops item. `pgdata` holds everything ingested,
   and Phase 1 adds a blob directory that also needs covering. Encrypted,
   off-machine, before content accumulates.
3. **Monitoring** — `/health` over the public domain, last verified webhook,
   failed-job count and queue age, and optionally a synthetic message. Not item
   freshness alone: a quiet week and a dead connector look identical.
4. **SSH hardening** — disable password and root login, after confirming key
   access from a second terminal.

---

## Cross-cutting, small

- **Vite proxy token.** The dev proxy forwards to the internal API without
  injecting `X-Internal-Token`. Loopback binding is doing the real work; decide
  deliberately whether that is the boundary.
- **`alembic check` in CI.** CI runs `alembic upgrade head` but never `check`,
  so a migration that has drifted from the models passes. The HNSW index is
  declared in both `models.py` and migration 0001 and must stay in step —
  exactly the drift `check` catches.

---

## What is left

```
2.2 OCR ──> 2.3 transcription   (blob store and media fetch are in place)
3.1 graph explorer ──> 3.2 insights
4 IMAP verification ──> 4 X bookmarks
5 recommender ──> 5 deep researcher
6 deploy ──> 6.2 backups ──> 6.3 monitoring ──> 6.4 SSH hardening
```

**Blocked on you, not on code:**

1. `CATCHMENT_WHATSAPP_ACCESS_TOKEN` — media downloads do nothing without it.
2. The Whisper decision (2.3): local faster-whisper as `CLAUDE.md` specifies,
   or Groq's hosted `whisper-large-v3-turbo`.
3. GitHub Actions is locked for billing, so **CI has never run** on this
   repository. Every check reported so far has been local.

**If only one operational thing gets done:** backups. Everything else here can
be rebuilt from the repository. Ingested content and blobs cannot.
