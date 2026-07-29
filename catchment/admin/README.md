# Admin (Appsmith)

Appsmith reads Postgres directly — there is no custom frontend and no admin API
to build here. This directory holds exported Appsmith application JSON and the
SQL the dashboard pages rely on, checked in so the dashboard is reproducible.

Layout:

- `apps/` — exported Appsmith application JSON (`Export application` in the UI)
- `queries/` — the SQL each page runs, kept readable and reviewable

## Review queue

The taxonomy review page is the one screen that performs writes. It must only:

1. read `taxonomy_proposals WHERE status = 'pending'`;
2. call approve/reject, which sets `status`, `reviewed_by`, `reviewed_at`.

Applying an approved merge is a backend job, not an Appsmith query — the
database rejects an `applied` row that was never approved
(`ck_proposals_applied_status`, `ck_proposals_reviewer_recorded`).

## Connection

Use a dedicated Postgres role for Appsmith with `SELECT` everywhere and
`UPDATE` limited to `taxonomy_proposals`. Credentials live in Appsmith's own
datasource config — never in this repository.
