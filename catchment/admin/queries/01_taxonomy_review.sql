-- Page 1: Taxonomy review — the only screen that writes.
-- Merges and splits are proposed here and applied only after a recorded
-- approval; the database rejects any other path (ck_proposals_*).

-- Q1: pending proposals, oldest first.
SELECT
    p.id,
    p.kind,
    p.rationale,
    p.proposed_by,
    p.created_at,
    p.payload,
    -- Resolve the tags named in the JSONB payload so the reviewer sees labels,
    -- not UUIDs. No FK exists on purpose: a proposal must survive the tags it
    -- mentions being merged away while it waits.
    (SELECT string_agg(t.label, ', ' ORDER BY t.label)
       FROM tags t
      WHERE t.id::text IN (
            SELECT jsonb_array_elements_text(p.payload -> 'source_tag_ids')
      )) AS source_labels,
    (SELECT t.label FROM tags t
      WHERE t.id::text = p.payload ->> 'target_tag_id') AS target_label
FROM taxonomy_proposals p
WHERE p.status = 'pending'
ORDER BY p.created_at
LIMIT 100;

-- Q2: approve. {{reviewer}} must be a real identity — a non-pending row
-- without one violates ck_proposals_reviewer_recorded.
UPDATE taxonomy_proposals
   SET status = 'approved', reviewed_by = {{reviewer}}, reviewed_at = now()
 WHERE id = {{proposal_id}} AND status = 'pending'
RETURNING id, status;

-- Q3: reject.
UPDATE taxonomy_proposals
   SET status = 'rejected', reviewed_by = {{reviewer}}, reviewed_at = now()
 WHERE id = {{proposal_id}} AND status = 'pending'
RETURNING id, status;

-- NOTE: applying an approved merge is a backend job, not an admin query.
-- The dashboard never sets status='applied' — mark_applied() refuses anything
-- not already approved, and ck_proposals_applied_status enforces it in the DB.
