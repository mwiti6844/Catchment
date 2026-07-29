"""Classifier decision paths, driven by a fixture (see tests/fixtures/)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from catchment.classification import ClassificationResult, TagSuggestion, slugify

FIXTURE = Path(__file__).parent / "fixtures" / "classification" / "tag_suggestions.json"


@pytest.fixture(scope="module")
def fixture_payload() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return payload


@pytest.fixture
def result(fixture_payload: dict[str, Any]) -> ClassificationResult:
    return ClassificationResult(
        suggestions=[
            TagSuggestion(
                label=item["label"],
                confidence=item["confidence"],
                is_new=item["is_new"],
                description=item.get("description"),
            )
            for item in fixture_payload["suggestions"]
        ],
        model=fixture_payload["model"],
        trace_id=fixture_payload["trace_id"],
    )


def test_fixture_labels_produce_expected_slugs(fixture_payload: dict[str, Any]) -> None:
    for item in fixture_payload["suggestions"]:
        suggestion = TagSuggestion(label=item["label"], confidence=item["confidence"])
        assert suggestion.slug == item["expected_slug"]


def test_threshold_filters_low_confidence(result: ClassificationResult) -> None:
    kept = result.above(0.5)

    assert len(kept) == 3
    assert all(s.confidence >= 0.5 for s in kept)
    assert "Café Culture" not in [s.label for s in kept]


def test_threshold_filtering_does_not_mutate(result: ClassificationResult) -> None:
    before = len(result.suggestions)
    result.above(0.9)
    assert len(result.suggestions) == before


def test_newly_coined_tags_are_flagged(result: ClassificationResult) -> None:
    new_tags = [s for s in result.suggestions if s.is_new]

    assert {s.slug for s in new_tags} == {"kenyan-fintech", "cafe-culture"}


def test_every_pass_carries_a_langfuse_trace(result: ClassificationResult) -> None:
    assert result.trace_id
    assert result.model


@pytest.mark.parametrize("confidence", [-0.01, 1.01, 2.0])
def test_out_of_range_confidence_is_rejected(confidence: float) -> None:
    with pytest.raises(ValueError, match=r"confidence must be in \[0, 1\]"):
        TagSuggestion(label="Anything", confidence=confidence)


def test_suggestions_are_immutable() -> None:
    suggestion = TagSuggestion(label="LLM Evaluation", confidence=0.9)
    with pytest.raises((AttributeError, TypeError)):
        suggestion.confidence = 0.1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Simple", "simple"),
        ("Multi   Word  Tag", "multi-word-tag"),
        ("Trailing---", "trailing"),
        ("C++ & Rust", "c-rust"),
        ("Ünïcödé", "unicode"),
    ],
)
def test_slugify_normalises(label: str, expected: str) -> None:
    assert slugify(label) == expected


@pytest.mark.parametrize("label", ["", "   ", "🙂", "…"])
def test_unsluggable_labels_are_rejected(label: str) -> None:
    with pytest.raises(ValueError, match="empty slug"):
        slugify(label)


def test_slug_is_length_bounded() -> None:
    assert len(slugify("word " * 200)) <= 128
