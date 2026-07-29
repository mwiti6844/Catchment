-- Page: Tags — the taxonomy table.
--
-- Parent comes from tag_edges, where `parent` is broader than `child`. A tag
-- may have several parents (the graph is not a tree), so parents are
-- aggregated rather than assumed singular.

SELECT
    t.label,
    t.slug,
    t.origin,
    t.status,
    t.description,
    t.created_at,
    (SELECT string_agg(p.label, ', ' ORDER BY p.label)
       FROM tag_edges e JOIN tags p ON p.id = e.parent_id
      WHERE e.child_id = t.id)                       AS parents,
    (SELECT count(*) FROM item_tags it WHERE it.tag_id = t.id) AS item_count,
    (SELECT count(*) FROM tag_edges e WHERE e.parent_id = t.id) AS child_count
FROM tags t
WHERE t.status = 'active'
ORDER BY item_count DESC, t.label;

-- Candidate near-duplicates: tags whose labels collapse to a similar slug.
-- This is the merge-proposal shortlist — the drift the review queue exists for.
-- Wrapped in a subselect: Postgres accepts a bare output alias in ORDER BY but
-- not one inside an expression, so `ORDER BY items_a + items_b` is an error.
SELECT *
FROM (
    SELECT a.label AS label_a, b.label AS label_b,
           (SELECT count(*) FROM item_tags WHERE tag_id = a.id) AS items_a,
           (SELECT count(*) FROM item_tags WHERE tag_id = b.id) AS items_b
    FROM tags a
    JOIN tags b
      ON a.id < b.id
     AND a.status = 'active' AND b.status = 'active'
     AND (b.slug LIKE a.slug || '%' OR a.slug LIKE b.slug || '%')
) pairs
ORDER BY items_a + items_b DESC
LIMIT 50;
