"""The classification decision path: embed, retrieve candidates, classify, assign.

This is where the dynamic taxonomy actually grows. The ordering matters: an item
is embedded and compared against neighbours *before* the model is asked, so the
prompt carries the tags already in use on similar content. Skipping that step is
what makes a classifier coin ``ML Ops`` next to an existing ``MLOps``.

See ``docs/taxonomy.md``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from catchment.classification.embeddings import Embedder
from catchment.classification.types import Classifier, TagSuggestion
from catchment.config import Settings, get_settings
from catchment.logging_config import get_logger, log_context
from catchment.storage.repositories import ItemRepository, TagRepository

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ClassificationOutcome:
    """What one classification pass did. Counts and ids only — never content."""

    item_id: uuid.UUID
    candidates: int
    suggested: int
    assigned: int
    coined: int
    model: str
    trace_id: str | None = None
    #: New tags dropped because the item hit its coinage cap.
    capped: int = 0
    #: Graph edges created placing a tag under a broader one.
    linked: int = 0

    @property
    def discarded(self) -> int:
        """Suggestions dropped for falling below the confidence threshold."""
        return self.suggested - self.assigned


def classify_item(
    *,
    items: ItemRepository,
    tags: TagRepository,
    embedder: Embedder,
    classifier: Classifier,
    item_id: uuid.UUID,
    text: str,
    settings: Settings | None = None,
) -> ClassificationOutcome:
    """Embed an item, classify it, and apply the resulting tags.

    Raises ``LookupError`` if the item is gone — the job outlived its row and
    retrying will not help.
    """
    resolved = settings or get_settings()

    if items.get(item_id) is None:
        raise LookupError(f"item {item_id} does not exist")

    vector = _embed(embedder, text)
    items.set_embedding(item_id=item_id, model=resolved.embedding_model, vector=vector)

    candidates = _candidate_tags(items, tags, vector=vector, item_id=item_id, settings=resolved)
    result = classifier.classify(text, known_tags=candidates)
    above_threshold = result.above(resolved.classification_threshold)
    kept, capped = _cap_new_tags(tags, above_threshold, limit=resolved.max_new_tags_per_item)

    coined, linked = _apply(
        tags,
        item_id=item_id,
        suggestions=kept,
        trace_id=result.trace_id,
        max_edges=resolved.max_new_edges_per_item,
    )

    outcome = ClassificationOutcome(
        item_id=item_id,
        candidates=len(candidates),
        suggested=len(result.suggestions),
        assigned=len(kept),
        coined=coined,
        model=result.model,
        trace_id=result.trace_id,
        capped=capped,
        linked=linked,
    )
    logger.info(
        "classification complete",
        extra=log_context(
            item_id=str(item_id),
            candidates=outcome.candidates,
            suggested=outcome.suggested,
            assigned=outcome.assigned,
            coined=outcome.coined,
            discarded=outcome.discarded,
            capped=outcome.capped,
            linked=outcome.linked,
            trace_id=outcome.trace_id,
        ),
    )
    return outcome


def _embed(embedder: Embedder, text: str) -> list[float]:
    vectors = embedder.embed([text])
    if not vectors:
        raise ValueError("embedder returned no vector for the item text")
    return vectors[0]


def _candidate_tags(
    items: ItemRepository,
    tags: TagRepository,
    *,
    vector: list[float],
    item_id: uuid.UUID,
    settings: Settings,
) -> list[str]:
    """Collect the tags already applied to the most similar items."""
    neighbours = items.nearest(
        vector=vector, limit=settings.classification_neighbours, exclude=item_id
    )
    return tags.labels_for_items([item.id for item, _distance in neighbours])


def _cap_new_tags(
    tags: TagRepository, suggestions: list[TagSuggestion], *, limit: int
) -> tuple[list[TagSuggestion], int]:
    """Bound how many genuinely new tags one item may coin.

    Prompt wording alone cannot stop injected content steering the classifier —
    ingested messages are written by other people. This bounds what a successful
    injection achieves: a couple of junk tags a human sees in review, not a
    flooded taxonomy.

    Novelty is decided against the *database*, not the candidate list: the
    candidates only cover nearby items, so a tag absent from them may still
    exist elsewhere in the graph. Capping on the candidate list would block
    legitimate reuse.
    """
    existing = tags.existing_slugs([s.slug for s in suggestions])

    kept: list[TagSuggestion] = []
    coined = 0
    capped = 0
    for suggestion in suggestions:
        if suggestion.slug not in existing:
            if coined >= limit:
                capped += 1
                continue
            coined += 1
        kept.append(suggestion)
    return kept, capped


def _apply(
    tags: TagRepository,
    *,
    item_id: uuid.UUID,
    suggestions: list[TagSuggestion],
    trace_id: str | None = None,
    max_edges: int,
) -> tuple[int, int]:
    """Create or reuse each tag, attach it, and place it in the graph.

    Returns ``(coined, linked)``. Placement is deliberately subordinate to
    assignment: an edge that cannot be created is skipped and the tag is still
    attached. The reverse — dropping a well-earned tag because its proposed
    parent was unusable — would lose information the classifier got right.
    """
    coined = 0
    linked = 0
    for suggestion in suggestions:
        tag, created = tags.get_or_create(
            slug=suggestion.slug,
            label=suggestion.label,
            description=suggestion.description,
            origin="llm",
        )
        coined += int(created)
        tags.assign(
            item_id=item_id,
            tag_id=tag.id,
            confidence=suggestion.confidence,
            assigned_by="llm",
            trace_id=trace_id,
        )
        if linked < max_edges and _place(tags, suggestion=suggestion, child_id=tag.id):
            linked += 1
    return coined, linked


def _place(
    tags: TagRepository, *, suggestion: TagSuggestion, child_id: uuid.UUID
) -> bool:
    """Link a tag under the broader one the classifier proposed, if it exists.

    The parent was validated at parse time as a label the model was shown, but
    a candidate label is not proof of a current row: the tag may have been
    merged away between the prompt and this write.
    """
    if suggestion.broader_than is None:
        return False

    parent = tags.get_by_slug(suggestion.broader_than)
    if parent is None or parent.id == child_id:
        return False

    return tags.link_broader(parent_id=parent.id, child_id=child_id)
