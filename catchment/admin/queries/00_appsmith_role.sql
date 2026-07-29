-- Provisions the restricted role Appsmith connects as. Idempotent: safe to
-- re-run on every stack start, which is how the appsmith-db-role compose
-- service applies it.
--
-- Grants are the enforcement of the review-gate invariant, not dashboard code.
-- Even a bug in Appsmith cannot touch items, tags, or extractions, because the
-- role holds no write privilege on them.
--
-- Requires: psql -v appsmith_password='...'

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'appsmith') THEN
        CREATE ROLE appsmith LOGIN;
    END IF;
END
$$;

-- Separate from CREATE so a re-run rotates the password rather than failing.
ALTER ROLE appsmith WITH PASSWORD :'appsmith_password';

GRANT CONNECT ON DATABASE catchment TO appsmith;
GRANT USAGE ON SCHEMA public TO appsmith;

-- Read everything.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO appsmith;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO appsmith;

-- Write exactly two things, on exactly the columns needed.
GRANT UPDATE (status, reviewed_by, reviewed_at) ON taxonomy_proposals TO appsmith;
GRANT UPDATE (resolved_at) ON pipeline_failures TO appsmith;

-- Revoke anything a previous, broader grant may have left behind, so re-running
-- this script converges on the documented privilege set rather than only adding
-- to it.
REVOKE INSERT, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON ALL TABLES IN SCHEMA public FROM appsmith;

-- Deliberately NOT granted:
--   * INSERT/DELETE anywhere — the dashboard never creates or removes rows.
--   * UPDATE on taxonomy_proposals.applied_at — applying a merge is a backend
--     job. ck_proposals_applied_status would reject it anyway, but the missing
--     grant makes the boundary explicit.
--   * Any write on items/tags/extractions/embeddings/item_tags.
