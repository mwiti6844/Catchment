-- Page 3: Dead letter — stages that degraded.
--
-- The pipeline deliberately degrades rather than failing: a classifier outage
-- still lands the item in the review queue tagged 'unclassified'. That keeps
-- ingestion resilient and makes the failure invisible, which is why
-- pipeline_failures exists. RQ's own failed-job registry lives in Redis and
-- The admin read model cannot read it directly.

-- Q1: open failures, oldest first.
SELECT
    f.id,
    f.stage,
    f.error_type,
    f.detail,
    f.occurred_at,
    i.id     AS item_id,
    i.source,
    i.kind,
    i.ingested_at
FROM pipeline_failures f
JOIN items i ON i.id = f.item_id
WHERE f.resolved_at IS NULL
ORDER BY f.occurred_at
LIMIT 200;

-- Q2: failure rate by type over the last 7 days — is this a blip or a trend?
SELECT stage, error_type, count(*) AS occurrences, max(occurred_at) AS last_seen
FROM pipeline_failures
WHERE occurred_at > now() - interval '7 days'
GROUP BY stage, error_type
ORDER BY occurrences DESC;

-- Q3: items stuck on the placeholder tag. An item here with no matching
--     pipeline_failures row had nothing to classify (a captionless image
--     awaiting OCR) rather than a classifier problem.
SELECT
    i.id AS item_id, i.source, i.kind, i.ingested_at,
    EXISTS (SELECT 1 FROM pipeline_failures f
             WHERE f.item_id = i.id AND f.resolved_at IS NULL) AS has_open_failure
FROM items i
JOIN item_tags it ON it.item_id = i.id
JOIN tags t ON t.id = it.tag_id AND t.slug = 'unclassified'
ORDER BY i.ingested_at DESC
LIMIT 200;

-- Q4: mark a failure handled (after a successful re-run).
UPDATE pipeline_failures
   SET resolved_at = now()
 WHERE id = {{failure_id}} AND resolved_at IS NULL
RETURNING id, resolved_at;
