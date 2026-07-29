-- Page 2: What the classifier has been doing.
-- The visibility gap this dashboard exists to close: the classifier coins tags
-- autonomously from live model output, and nothing else shows you what it chose.

-- Q1: recent assignments with confidence and provenance.
--     assigned_by tells you a model decision ('llm') from a degraded fallback
--     or backfill ('import') — they are not the same event.
SELECT
    i.id            AS item_id,
    i.source,
    i.kind,
    i.author,
    i.ingested_at,
    t.label         AS tag,
    t.origin        AS tag_origin,
    it.confidence,
    it.assigned_by,
    it.created_at   AS assigned_at,
    -- Length only. Item text is personal correspondence and must not be
    -- rendered in a dashboard that is open on a screen all day.
    length(e.text)  AS extracted_chars
FROM item_tags it
JOIN items i ON i.id = it.item_id
JOIN tags  t ON t.id = it.tag_id
LEFT JOIN extractions e ON e.item_id = i.id
ORDER BY it.created_at DESC
LIMIT 200;

-- Q2: newly coined tags — the taxonomy growing. Watch this for near-duplicates
--     of existing tags; that is what a merge proposal is for.
SELECT
    t.label,
    t.slug,
    t.description,
    t.origin,
    t.created_at,
    count(it.item_id) AS items_tagged
FROM tags t
LEFT JOIN item_tags it ON it.tag_id = t.id
WHERE t.status = 'active'
GROUP BY t.id
ORDER BY t.created_at DESC
LIMIT 100;

-- Q3: low-confidence assignments — the ones most worth a human eye.
SELECT i.id AS item_id, i.source, t.label AS tag, it.confidence, it.created_at
FROM item_tags it
JOIN items i ON i.id = it.item_id
JOIN tags  t ON t.id = it.tag_id
WHERE it.assigned_by = 'llm' AND it.confidence < 0.7
ORDER BY it.confidence ASC, it.created_at DESC
LIMIT 100;
