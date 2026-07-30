-- Pages: Inbox and Item detail.
--
-- Item text IS rendered on the detail page — that is the point of it — but the
-- Inbox list shows only metadata and a length, so the default screen does not
-- put correspondence on display.

-- Q1 (Inbox): recent items with classification status.
--     status distinguishes three states the placeholder tag alone cannot:
--     classified by a model, degraded to the fallback, or nothing to classify.
SELECT
    i.id,
    i.source,
    i.kind,
    i.author,
    i.ingested_at,
    length(e.text)                       AS extracted_chars,
    (emb.item_id IS NOT NULL)            AS has_embedding,
    CASE
        WHEN EXISTS (SELECT 1 FROM item_tags it2
                      JOIN tags t2 ON t2.id = it2.tag_id
                     WHERE it2.item_id = i.id AND it2.assigned_by = 'llm')
            THEN 'classified'
        WHEN EXISTS (SELECT 1 FROM pipeline_failures f
                     WHERE f.item_id = i.id AND f.resolved_at IS NULL)
            THEN 'failed'
        WHEN e.id IS NULL THEN 'nothing to classify'
        ELSE 'pending'
    END                                  AS classification_status
FROM items i
LEFT JOIN extractions e ON e.item_id = i.id
LEFT JOIN embeddings  emb ON emb.item_id = i.id
ORDER BY i.ingested_at DESC
LIMIT 200;

-- Q2 (Item detail): the extracted text. Bind {{item_id}}.
SELECT i.id, i.source, i.source_id, i.kind, i.author, i.url,
       i.published_at, i.ingested_at, i.meta,
       e.extractor, e.language, e.confidence, e.text,
       (emb.item_id IS NOT NULL) AS has_embedding,
       emb.model                 AS embedding_model,
       emb.dim                   AS embedding_dim
FROM items i
LEFT JOIN extractions e ON e.item_id = i.id
LEFT JOIN embeddings emb ON emb.item_id = i.id
WHERE i.id = {{item_id}};

-- Q3 (Item detail): tags with confidence, and the Langfuse trace that produced
--     each one. trace_id is null for rule-based assignments — a fallback has no
--     model call to link to, which is itself the useful signal.
--     A UI can build:
--       <langfuse-base>/project/<project>/traces/<trace_id>
SELECT t.label, t.slug, t.origin, it.confidence, it.assigned_by,
       it.trace_id, it.created_at
FROM item_tags it
JOIN tags t ON t.id = it.tag_id
WHERE it.item_id = {{item_id}}
ORDER BY it.confidence DESC;
