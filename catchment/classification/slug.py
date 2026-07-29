"""Slug derivation for tags.

Tag slugs are the identity used for deduplication when the classifier coins a
tag, so the mapping from label to slug must be stable and deterministic.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

MAX_SLUG_LENGTH: Final[int] = 128

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(label: str) -> str:
    """Return a stable, URL-safe slug for a tag label.

    Raises ``ValueError`` when the label contains nothing sluggable, rather
    than silently producing an empty slug that would collide across tags.
    """
    normalized = unicodedata.normalize("NFKD", label)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _NON_ALNUM.sub("-", ascii_only).strip("-")

    if not slug:
        raise ValueError(f"label {label!r} produces an empty slug")
    return slug[:MAX_SLUG_LENGTH].rstrip("-")
