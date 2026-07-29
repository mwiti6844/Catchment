-- Run once as a superuser. Appsmith connects as this role, never as the
-- application user.
--
-- The review-gate invariant is enforced by grants, not by dashboard code: even
-- a bug in Appsmith cannot touch items, tags, or extractions, because the role
-- has no write privilege on them. The only writes it can perform are recording
-- a taxonomy decision and closing a failure.

CREATE ROLE appsmith LOGIN PASSWORD :'appsmith_password';

GRANT CONNECT ON DATABASE catchment TO appsmith;
GRANT USAGE ON SCHEMA public TO appsmith;

-- Read everything.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO appsmith;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO appsmith;

-- Write exactly two things, on exactly the columns needed.
GRANT UPDATE (status, reviewed_by, reviewed_at) ON taxonomy_proposals TO appsmith;
GRANT UPDATE (resolved_at) ON pipeline_failures TO appsmith;

-- Deliberately NOT granted:
--   * INSERT/DELETE anywhere — the dashboard never creates or removes rows.
--   * UPDATE on taxonomy_proposals.applied_at — applying a merge is a backend
--     job. ck_proposals_applied_status would reject it anyway, but the grant
--     makes the boundary explicit rather than relying on a constraint to catch
--     a query that should never have been written.
