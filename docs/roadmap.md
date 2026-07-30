# Roadmap

Everything not yet built, sequenced by dependency rather than by appeal.

State this is written against: the ingestion spine is proven end-to-end
(WhatsApp → item → extraction → embedding → classification → tags), traced in
Langfuse, with a coded dashboard on a loopback-only API. What follows is
everything else.

The ordering principle throughout: **finish what is half-built before starting
what is unbuilt.** Two features shipped in the last slices are currently inert
for want of a small piece of upstream work, and that is the cheapest value in
the whole document.

---

## Phase 0 — Make what already exists actually work

Two gaps where the code is written and tested but has no data to act on. Both
are small. Both unblock things already paid for.

### 0.1 Tag edge creation

**Problem.** `TagRepository.add_edge` is never called. The graph has
`tags=3, tag_edges=0`. Consequences: the graph expansion in search reaches
nothing, so retrieval currently behaves as vector-only; and a graph explorer
would render disconnected dots.

**Work.** When the classifier coins a tag, ask it whether the new concept is
narrower than an existing one, and write the edge. Extend the prompt schema
with an optional `broader_than` / `narrower_than` field, parse it with the same
drop-unusable-entries discipline, and call `add_edge` in
`classification/service.py`.

**Risks.** The graph is not guaranteed acyclic and traversals are already
depth-bounded, so a bad edge degrades results rather than hanging anything.
Cap edges per item like `max_new_tags_per_item` caps coinage — an injected item
should not be able to rewire the taxonomy.

**Done when.** A fixture covers the new decision path (per `CLAUDE.md`), an
integration test walks a real two-level graph, and search returns at least one
`route=expanded` hit against live data.

### 0.2 Merge and split executor

**Problem.** `mark_applied` is called nowhere outside its own definition. The
Review page can record approvals that never take effect — the queue is
write-only.

**Work.** An RQ job consuming approved proposals. `docs/taxonomy.md` already
specifies it: in one transaction, repoint `item_tags` to the target keeping the
higher confidence on collision, repoint `tag_edges`, set each source tag
`status='merged'` with `merged_into_id`, then `mark_applied`. Splits are the
inverse and are rarer — ship merge first.

**Risks.** This is the only operation that rewrites history across many items.
It must be one transaction, and it must be idempotent, because a job retried
after a partial failure would otherwise double-apply. Source tags are retained,
never deleted, so old assignments stay interpretable.

**Done when.** An integration test approves a proposal, runs the job, and
asserts the item set moved, the source tag is `merged`, and re-running the job
is a no-op.

---

## Phase 1 — The media pipeline

**This is the real prerequisite for all extraction, and it is not obvious.**

`raw_ref` is documented as "pointer to blob storage". No blob storage exists.
For WhatsApp media, `raw_ref` holds a *Meta media ID* — a pointer into their
API, not something any extractor can open. `Extractor.extract(raw_ref)`
promises "the blob at raw_ref", which has never been true. Every media item so
far has been text, so nothing has surfaced this.

Nothing in Phase 2's extraction work can start until this exists.

### 1.1 Blob storage

**Work.** A small storage abstraction with a local-filesystem implementation
behind it — a `BlobStore` protocol with `put(key, bytes) -> ref` and
`open(ref)`. Filesystem is right for a personal pipeline; the protocol means
S3/MinIO later is one implementation, not a rewrite.

**Constraint.** Media is personal correspondence. It lives outside the database
deliberately (`docs/schema.md`), must not enter container images, and the
directory needs the same gitignore treatment as `.env`.

### 1.2 WhatsApp media fetch

**Work.** An RQ job that resolves a Meta media ID to a download URL, fetches
the bytes, stores them, and rewrites `raw_ref` to the blob reference.

**Blocker to flag.** This needs `META_WHATSAPP_ACCESS_TOKEN`, which was
deliberately deleted from `.env` on the grounds that "inbound webhook operation
does not require it". That was correct then and is wrong for this phase —
media download does require it. It is also a *rotating* token unless a
permanent one is issued, so the credential story needs deciding, not just the
value restoring.

**Risks.** Media URLs from Meta are short-lived. Fetch must happen promptly
after receipt or the reference expires — which argues for enqueuing the fetch
from the webhook path, alongside the existing pipeline job.

---

## Phase 2 — Real extraction

Replaces the passthrough extractor. Depends entirely on Phase 1.

### 2.1 Article parsing (start here)

`trafilatura` over `url`, for links and Substack items. **No blob storage
needed** — it fetches the URL directly, so this is the one extraction task that
could ship before Phase 1 if you want early value.

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

Deferred from v1 on purpose. **Depends on Phase 0.1** — both features need a
graph that has edges.

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
| **Substack RSS** | Easiest: no auth, plain feed parsing, pairs naturally with 2.1 article parsing. Do first. |
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

## Suggested order

```
0.1 tag edges ──┬──> 3.1 graph explorer
                └──> search expansion becomes real
0.2 merge executor ──> review queue becomes useful
2.1 article parsing (no dependencies — early value)
1.1 blob store ──> 1.2 media fetch ──> 2.2 OCR ──> 2.3 transcription
4 Substack ──> 4 IMAP verification ──> 4 X
6.2 backups (do before content accumulates, not after)
3.2 insights ──> 5 agents
```

**If only one thing gets done:** Phase 0.1. It is the smallest change that
makes two already-shipped features stop being decorative.

**If only one operational thing gets done:** backups. Everything else on this
list can be rebuilt from the repository. Ingested content cannot.
