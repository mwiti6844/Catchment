"""Types exchanged between the classifier and the storage layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from catchment.classification.slug import slugify


@dataclass(frozen=True, slots=True)
class TagSuggestion:
    """A tag the classifier wants attached to an item.

    ``is_new`` records whether the classifier believes it is coining a tag;
    the repository still decides, since another item may have created the same
    slug concurrently.

    ``broader_than`` is the *slug* of an existing tag this one belongs under.
    It is already validated by the time it lands here: the parser only keeps a
    parent the model was actually shown, so an item cannot name an arbitrary
    tag and graft itself onto it.
    """

    label: str
    confidence: float
    is_new: bool = False
    description: str | None = None
    broader_than: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")

    @property
    def slug(self) -> str:
        return slugify(self.label)


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """Everything one classification pass produced for an item."""

    suggestions: list[TagSuggestion] = field(default_factory=list)
    model: str = ""
    trace_id: str | None = None

    def above(self, threshold: float) -> list[TagSuggestion]:
        """Return suggestions meeting a confidence threshold. Non-mutating."""
        return [s for s in self.suggestions if s.confidence >= threshold]


@runtime_checkable
class Classifier(Protocol):
    """Assigns tags to an item's extracted text.

    Every implementation routes its LLM calls through Langfuse and returns the
    resulting ``trace_id`` so a decision can be audited later.
    """

    def classify(self, text: str, *, known_tags: list[str]) -> ClassificationResult:
        """Return tag suggestions for ``text``."""
        ...
