"""Schema-level invariants, asserted against the mapped metadata.

These run without a database: they check that the guarantees live in the
schema rather than in application code.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CheckConstraint, Table, UniqueConstraint

from catchment.storage.models import (
    EMBEDDING_DIM,
    Base,
    Embedding,
    Item,
    PipelineFailure,
    TaxonomyProposal,
)


def _table(model: type[Any]) -> Table:
    """``__table__`` is typed as FromClause on the declarative base."""
    return cast(Table, model.__table__)


def _unique_columns(table: Table) -> set[frozenset[str]]:
    return {
        frozenset(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _check_names(table: Table) -> set[str]:
    return {
        str(constraint.name)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }


def test_ingestion_dedupe_is_a_database_constraint() -> None:
    assert frozenset({"source", "source_id"}) in _unique_columns(_table(Item))


def test_source_and_kind_are_constrained() -> None:
    assert {"ck_items_source", "ck_items_kind"} <= _check_names(_table(Item))


def test_one_embedding_per_item() -> None:
    assert _table(Embedding).c.item_id.unique is True
    assert _table(Embedding).c.vector.type.dim == EMBEDDING_DIM  # type: ignore[attr-defined]


def test_proposals_require_a_recorded_reviewer() -> None:
    assert "ck_proposals_reviewer_recorded" in _check_names(_table(TaxonomyProposal))


def test_proposals_cannot_be_applied_without_approval() -> None:
    assert "ck_proposals_applied_status" in _check_names(_table(TaxonomyProposal))


def test_proposals_default_to_pending() -> None:
    default = _table(TaxonomyProposal).c.status.default
    assert default is not None
    assert getattr(default, "arg", None) == "pending"


def test_media_bytes_are_not_stored_in_items() -> None:
    """Only a pointer to blob storage lives on the row."""
    assert "raw_ref" in _table(Item).c
    assert not {"blob", "bytes", "data"} & set(_table(Item).c.keys())


def test_expected_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "items",
        "extractions",
        "embeddings",
        "tags",
        "tag_edges",
        "item_tags",
        "taxonomy_proposals",
        "pipeline_failures",
    }


def test_failures_are_constrained_to_known_stages() -> None:
    assert "ck_failures_stage" in _check_names(_table(PipelineFailure))


def test_open_failures_are_indexed_for_the_review_queue() -> None:
    """The dead-letter pane filters on resolved_at IS NULL."""
    names = {index.name for index in _table(PipelineFailure).indexes}
    assert "ix_failures_open" in names


def test_failures_carry_no_content_column() -> None:
    """`detail` holds an exception class or status, never a provider message."""
    columns = set(_table(PipelineFailure).c.keys())
    assert not {"text", "body", "content", "prompt"} & columns
