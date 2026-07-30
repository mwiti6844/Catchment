# Admin interface

The admin UI is not currently implemented. This directory retains the
database read queries and backend contracts needed by a replacement.

## Query catalogue

```text
queries/
  01_taxonomy_review.sql     pending merge/split proposals
  02_recent_assignments.sql  classifier assignments and coined tags
  03_dead_letter.sql         degraded stages and resolution
  04_inbox_and_detail.sql    inbox, item text, tags, and trace provenance
  05_taxonomy.sql            taxonomy and near-duplicate candidates
```

These queries define the intended read models for Inbox, Item detail, Tags,
Failures, and Review. Queue state is read from Redis through
`GET /internal/queue`.

## Decision boundary

Approve and Reject actions must go through
`POST /internal/proposals/{proposal_id}/decision`, authenticated with
`X-Internal-Token`. The repository performs a compare-and-swap decision
(`WHERE status = 'pending'`) so concurrent reviewers cannot overwrite one
another. Applying an approved merge remains a backend job.

The default Inbox should show metadata and extracted character counts rather
than personal correspondence. Full extracted text belongs only on Item detail.
Model-derived tag assignments carry their persisted Langfuse `trace_id`;
rule-based fallbacks correctly have no trace.

Any replacement UI must remain private: it displays personal WhatsApp and
email content, and `/internal/*` is deliberately blocked by Caddy.
