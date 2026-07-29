# Admin (Appsmith)

The local admin surface has six pages: **Inbox**, **Item detail**, **Tags**,
**Failures**, **Review**, and **Queue**. Appsmith is bound to
`127.0.0.1:8080`; it is never routed through Caddy or the development tunnel.

The page-by-page canvas specification, datasource settings, REST bindings, and
export procedure live in
[`appsmith-export/README.md`](appsmith-export/README.md). Treat that file as the
canonical build guide.

## Version-controlled pieces

```text
queries/
  00_appsmith_role.sql       restricted database role, converged on startup
  01_taxonomy_review.sql     pending merge/split proposals
  02_recent_assignments.sql  classifier assignments and coined tags
  03_dead_letter.sql         degraded stages and resolution
  04_inbox_and_detail.sql    inbox, item text, tags, and trace provenance
  05_taxonomy.sql            taxonomy and near-duplicate candidates
appsmith-export/
  README.md                  canonical six-page canvas specification
  application.json           created by Appsmith Export after the first build
```

`application.json` is deliberately absent until the application has been built
once in Appsmith's browser editor. Its internal widget tree and binding IDs
must not be fabricated by hand.

## Data and decision boundaries

Appsmith uses two datasources:

1. `catchment-db`, connected as the restricted `appsmith` PostgreSQL role;
2. `catchment-internal`, connected to `http://api:8000/internal` with
   `X-Internal-Token`.

The database role can read all tables, update only the decision columns on
`taxonomy_proposals`, and set only `pipeline_failures.resolved_at`. It cannot
insert or delete rows, modify items/tags/extractions, or set
`taxonomy_proposals.applied_at`.

Approve and Reject actions nevertheless go through the authenticated REST
endpoint, not raw SQL. The repository performs a compare-and-swap decision
(`WHERE status = 'pending'`) so concurrent reviewers cannot overwrite one
another. Applying an approved merge remains a backend job.

The default Inbox shows metadata and extracted character counts rather than
personal correspondence. Full extracted text appears only on Item detail.
Model-derived tag assignments carry their persisted Langfuse `trace_id`;
rule-based fallbacks correctly have no trace.

Credentials live only in `.env` and Appsmith's datasource configuration. They
must never appear in the application export.
