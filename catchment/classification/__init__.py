"""Embedding and dynamic tag assignment.

The tag graph has no fixed taxonomy: the classifier may assign existing tags
or coin new ones. What it may *not* do is restructure the graph — merges and
splits go to the review queue in ``storage/repositories.py``. See
``docs/taxonomy.md``.
"""

from catchment.classification.slug import slugify
from catchment.classification.types import ClassificationResult, Classifier, TagSuggestion

__all__ = [
    "ClassificationResult",
    "Classifier",
    "TagSuggestion",
    "slugify",
]
