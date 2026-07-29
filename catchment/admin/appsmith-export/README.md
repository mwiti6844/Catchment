# Appsmith application export

**This directory is empty of an export, deliberately — see below.**

## Why there is no `application.json` here yet

Appsmith applications are built in its browser page-builder. The export format
is an internal, versioned representation of that canvas: widget trees, layout
geometry, dynamic-binding paths, datasource references and per-instance IDs.

It cannot be authored by hand with any confidence. A fabricated file either
fails to import or — worse — imports into a dashboard that looks right and
silently mis-binds a query, which on the Review page means an approve button
wired to the wrong proposal. An absent export is safer than a plausible one.

So the export has to come from a real Appsmith instance, built once through
the UI. **Everything needed to build it is version-controlled**: the SQL is in
`../queries/` and verified against the live schema, the REST contracts are in
`catchment/internal_api.py` and covered by tests, and the page-by-page spec is
below. What is missing is only the canvas layout.

## Producing it

1. Bring the stack up and open <http://localhost:8080>.
2. Create the two datasources (below), then build the six pages from the spec.
3. **Export**: application menu (⋮) → *Export application* → save the
   downloaded JSON here as `application.json` and commit it.
4. From then on a fresh instance is reproducible: *Import* → select that file →
   re-enter the two datasource passwords (Appsmith does not export credentials,
   which is correct).

---

## Datasources

### 1. `catchment-db` — PostgreSQL

| Field | Value |
| --- | --- |
| Host | `postgres` |
| Port | `5432` |
| Database | `catchment` |
| Username | `appsmith` |
| Password | `CATCHMENT_APPSMITH_DB_PASSWORD` from `.env` |

**Never the application's own credentials.** This role holds `SELECT`
everywhere and `UPDATE` on only the decision columns of `taxonomy_proposals`
and `resolved_at` on `pipeline_failures` — verified empirically, not just
granted. A bug in the dashboard cannot touch items, tags or extractions.

### 2. `catchment-internal` — Authenticated API

| Field | Value |
| --- | --- |
| URL | `http://api:8000/internal` |
| Header | `X-Internal-Token: <CATCHMENT_INTERNAL_API_TOKEN>` |

Reached over the compose network, so it never traverses Caddy — which refuses
`/internal/*` from the public path anyway.

---

## Pages

| Page | Source | Notes |
| --- | --- | --- |
| **Inbox** | `queries/04_inbox_and_detail.sql` Q1 | Metadata + `classification_status`. Shows `extracted_chars`, not text — the default screen should not display correspondence. Row click → Item detail with `item_id`. |
| **Item detail** | Q2 (item + text), Q3 (tags) | Q3 returns `trace_id` per tag. Link out with `{{appsmith.store.langfuseBase}}/project/catchment/traces/{{currentRow.trace_id}}`; disable the link when `trace_id` is null — a rule-based fallback has no model call, which is itself the signal. |
| **Tags** | `queries/05_taxonomy.sql` | Taxonomy table with parents, item counts, origin. Second query is the near-duplicate shortlist — the merge-proposal candidates. |
| **Failures** | `queries/03_dead_letter.sql` | Open failures, 7-day rates, and items stuck on the placeholder tag. Q4 resolves one. |
| **Review** | `queries/01_taxonomy_review.sql` Q1 for the list; **REST for the decision** | Approve/Reject must `POST {{catchment-internal}}/proposals/{{id}}/decision` with `{"decision":"approve","reviewer":"<you>"}`. **Do not** write a raw `UPDATE` here — see below. |
| **Queue** | `GET {{catchment-internal}}/queue` | pending / started / finished / failed / deferred / scheduled, plus `oldest_pending_seconds`. Appsmith has no Redis connector; this is why the endpoint exists. |

### Why Review goes through REST rather than SQL

The `appsmith` role *can* update the decision columns, so a raw
`UPDATE taxonomy_proposals SET status='approved' ...` would appear to work.
It would lose the compare-and-swap. The repository decides with
`UPDATE ... WHERE status='pending' RETURNING`, so a second reviewer racing the
first gets a conflict instead of silently overwriting a recorded decision.
A plain `UPDATE` has no such guard.

The SQL grant remains as defence in depth — it bounds what a mistake can reach —
but the intended path is the endpoint.
