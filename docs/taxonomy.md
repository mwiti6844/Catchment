# Taxonomy

The tag graph has no fixed vocabulary. It grows as content arrives, and the
interesting design problem is keeping it from degenerating into either a flat
pile of synonyms or an unnavigable tangle.

## The three operations

| Operation | Who performs it | Where |
| --- | --- | --- |
| Assign an existing tag | classifier, automatically | `TagRepository.assign` |
| Coin a new tag | classifier, automatically | `TagRepository.get_or_create` |
| Merge or split tags | **human, after review** | `TaxonomyProposalRepository` |

The split matters: assignment and coinage are additive and cheap to undo. A
merge rewrites history across every item that carried the tag. So merges and
splits are *proposed*, queued, and applied only after approval.

## Assignment

1. Embed the item's extracted text with BGE-M3.
2. Retrieve nearby items and collect the tags already applied to them — this
   is the candidate set, so the classifier sees the vocabulary in use rather
   than inventing in a vacuum.
3. Ask the model for tags with confidences, passing the candidates as
   `known_tags`.
4. Keep suggestions above the confidence threshold; store the rest nowhere.

Every call runs through Langfuse, and the resulting `trace_id` is carried on
`ClassificationResult` so any assignment can be traced back to the prompt that
produced it.

## Coining a new tag

A suggestion marked `is_new` still goes through `get_or_create`, which inserts
on the unique `slug` and returns the existing row on conflict. Two items
classified concurrently that both invent "Kenyan Fintech" converge on one tag;
the database decides, not the classifier.

Slugs come from `slugify`, which is deterministic and lossy in a useful way:
`"Café Culture"`, `"cafe culture"` and `"CAFE  CULTURE"` all collapse to
`cafe-culture`. Labels with nothing sluggable (emoji, punctuation only) raise
rather than producing an empty slug that would collide with every other empty
slug.

## Placing a tag in the graph

The classifier returns an optional `broader_than` alongside each suggestion:
the label of a more general tag this one belongs under. Two rules govern it,
both about containment rather than accuracy.

The proposed parent **must be a tag the model was actually shown**. Accepting
any label the response names would let an ingested message attach a tag
anywhere in the graph. The candidate list is the whitelist.

**Edges per item are capped** (`CATCHMENT_MAX_NEW_EDGES_PER_ITEM`, default 4),
for the same reason coinage is. The cap on edges matters more: a coined junk
tag sits in review, while an edge changes what *retrieval* reaches for every
item carrying those tags.

`TagRepository.link_broader` refuses an edge whose parent is already reachable
below the child — that edge would close a cycle. A refusal never costs the
assignment that came with it: losing an edge is cheap, losing a well-earned tag
is not. The graph is a DAG by intent but not by
enforcement — nothing stops a sequence of individually reasonable edges from
closing a cycle.

This is why **every traversal is depth-bounded**. `TagRepository.ancestors` and
`.descendants` take a `max_depth`, defaulted from
`CATCHMENT_MAX_TAG_DEPTH` and hard-capped at `TAG_DEPTH_HARD_CEILING`. The
bound lands in the recursive term of the CTE, so a cycle returns `max_depth`
rows and stops. There is no code path that walks the graph without a bound.

## Merges and splits

Drift shows up in predictable ways:

- **Synonyms** — `ml-ops` and `mlops` accumulating separately.
- **Overloaded tags** — one tag spanning two distinct clusters in embedding
  space (the signal for a split).
- **Near-duplicate siblings** — tags whose item sets almost coincide.

When the classifier detects one, it writes a proposal:

```python
proposals.propose_merge(
    source_tag_ids=[ml_ops.id],
    target_tag_id=mlops.id,
    rationale="identical item sets; label differs by punctuation only",
)
```

Nothing else happens. The row lands `pending`, the admin review surface lists
it, and a human approves or rejects with their identity recorded.

### The lifecycle

```
pending ──approve──> approved ──(job runs the merge)──> applied
   │
   └────reject────> rejected
```

`mark_applied` raises `ProposalNotApprovedError` for anything not currently
`approved`, and the database refuses an `applied_at` on a non-`applied` row.
Skipping review is not something the code declines to do — it is not
representable.

### Applying an approved merge

`catchment/taxonomy/apply.py` consumes approved proposals. In one transaction
it repoints `item_tags` from the source tags to the target (keeping the higher
confidence on collision), repoints `tag_edges`, sets each source tag's
`status = 'merged'` and `merged_into_id`, then calls `mark_applied`. Source
tags are retained, never deleted, so old assignments remain interpretable.

Repointing edges has one trap worth naming: if the target was already the
source's parent, a blind rewrite produces `target -> target`, which a check
constraint refuses. Those edges are dropped instead — the relationship they
described disappears with the merge.

Each proposal runs in its own savepoint, so one unapplicable proposal (a tag
deleted since approval, a payload that no longer parses) is logged and left
`approved` rather than stalling every proposal queued behind it.

Approving through the dashboard applies the merge in the same transaction that
records the decision. The gate is that a human decides *before* the merge runs,
not that the two are separated in time — separating them is what left approved
changes sitting in a queue with nothing to execute them.

**Splits are not executed.** The payload names the new tags but not which items
go where, and guessing that would be a taxonomy decision made by code. The
executor raises rather than approximating it.

## Adding a decision path

Per `CLAUDE.md`, every new classifier decision path needs a test with a fixture
in `catchment/tests/fixtures/`. `fixtures/classification/tag_suggestions.json`
is the pattern: label, confidence, `is_new`, and the `expected_slug` the
decision should produce.
