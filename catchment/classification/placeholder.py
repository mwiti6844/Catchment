"""Placeholder classification for the first end-to-end slice.

This is **not** the classifier. It assigns every item one ``unclassified``
tag so the pipeline produces an ``item_tags`` row and the Appsmith queue has
something in it, which is what makes the chain observable before any model
weights exist. Real classification — embedding similarity, candidate
retrieval, LLM tag creation — replaces this and is described in
``docs/taxonomy.md``.
"""

from __future__ import annotations

import uuid
from typing import Final

from catchment.storage.repositories import TagRepository

UNCLASSIFIED_SLUG: Final[str] = "unclassified"
UNCLASSIFIED_LABEL: Final[str] = "Unclassified"
UNCLASSIFIED_DESCRIPTION: Final[str] = (
    "Placeholder applied by the ingestion pipeline before the classifier runs. "
    "Not a concept — a marker for items awaiting classification."
)


def assign_unclassified(*, tags: TagRepository, item_id: uuid.UUID) -> uuid.UUID:
    """Attach the ``unclassified`` tag to an item and return the tag id.

    ``origin``/``assigned_by`` are ``import`` rather than ``llm``: this is a
    deterministic rule, and mislabelling it as a model decision would corrupt
    the provenance the tag graph relies on when the real classifier lands.
    """
    tag, _ = tags.get_or_create(
        slug=UNCLASSIFIED_SLUG,
        label=UNCLASSIFIED_LABEL,
        description=UNCLASSIFIED_DESCRIPTION,
        origin="import",
    )
    tags.assign(
        item_id=item_id, tag_id=tag.id, confidence=1.0, assigned_by="import"
    )
    return tag.id
