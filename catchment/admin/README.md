# Admin (Appsmith)

Appsmith reads Postgres directly — there is no custom frontend and no admin API
to build here. This directory holds the SQL each page runs, checked in so the
dashboard is reproducible, plus exported Appsmith application JSON.

```
queries/
  00_appsmith_role.sql      run once as superuser; the grants below
  01_taxonomy_review.sql    page 1 — proposals awaiting approval
  02_recent_assignments.sql page 2 — what the classifier has been doing
  03_dead_letter.sql        page 3 — stages that degraded
apps/                       exported application JSON ("Export application")
```

Every read query in `queries/` has been executed against the live schema.

## Why this dashboard exists

The classifier coins tags autonomously from live model output. Without a
review surface you have no way to see what it chose, which near-duplicates it
created, or which items it silently failed to classify. Those are the three
pages.

## Page 1 — Taxonomy review

The only screen that writes. Lists `taxonomy_proposals` where
`status = 'pending'`, resolving the tag UUIDs inside the JSONB payload to
labels so a reviewer sees names.

Approve/reject sets `status`, `reviewed_by`, `reviewed_at`. **Applying an
approved merge is a backend job, not an Appsmith query** — the dashboard never
writes `status = 'applied'`. `mark_applied()` refuses anything not already
approved, and `ck_proposals_applied_status` enforces it in the database.

## Page 2 — Recent assignments

Recent `item_tags` with confidence and provenance. `assigned_by` distinguishes
a model decision (`llm`) from a degraded fallback or backfill (`import`) —
these are different events and should not be read as the same thing.

Also: newly coined tags with item counts (watch for near-duplicates — that is
what a merge proposal is for), and a low-confidence pane, which is where human
attention is worth most.

**Item text is never rendered** — only `length(e.text)`. This is personal
correspondence and the dashboard is open on a screen all day.

## Page 3 — Dead letter

The pipeline degrades rather than fails: a classifier outage still lands the
item, tagged `unclassified`. That keeps ingestion resilient and makes the
failure invisible, which is why `pipeline_failures` exists.

Note that **RQ's own failed-job registry lives in Redis and Appsmith cannot
read it**. `pipeline_failures` is the Postgres-visible record; a job that dies
outright (rather than degrading) still only appears in RQ.

An item on the `unclassified` tag with no open failure row had nothing to
classify — a captionless image awaiting OCR — rather than a classifier problem.
Query 3 on that page distinguishes the two.

## Connection

Appsmith connects as the `appsmith` role from `queries/00_appsmith_role.sql`,
never as the application user. The review-gate invariant is enforced by
**grants**, not by dashboard code:

| Privilege | Scope |
| --- | --- |
| `SELECT` | every table |
| `UPDATE (status, reviewed_by, reviewed_at)` | `taxonomy_proposals` only |
| `UPDATE (resolved_at)` | `pipeline_failures` only |
| `INSERT` / `DELETE` | **nowhere** |

So a bug in the dashboard cannot touch items, tags, or extractions at all.
`applied_at` is deliberately not grantable: a constraint would reject it
anyway, but the missing grant makes the boundary explicit rather than relying
on the constraint to catch a query that should never have been written.

Credentials live in Appsmith's own datasource config — never in this repository.
