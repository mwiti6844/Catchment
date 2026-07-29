"""Recursive walks over the tag graph must always carry an explicit depth bound."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql

from catchment.config import TAG_DEPTH_HARD_CEILING
from catchment.storage.repositories import (
    UnboundedTraversalError,
    build_ancestors_stmt,
    build_descendants_stmt,
)

TAG_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")

WalkBuilder = Callable[[uuid.UUID, int], Select[tuple[uuid.UUID, int]]]

BUILDERS: list[WalkBuilder] = [build_ancestors_stmt, build_descendants_stmt]


def _compile(stmt: Select[Any]) -> tuple[str, dict[str, Any]]:
    compiled = stmt.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    return str(compiled).upper(), dict(compiled.params)


@pytest.mark.parametrize("builder", BUILDERS)
def test_walk_is_recursive(builder: WalkBuilder) -> None:
    sql, _ = _compile(builder(TAG_ID, 5))
    assert "RECURSIVE" in sql


@pytest.mark.parametrize("builder", BUILDERS)
def test_walk_emits_a_depth_predicate(builder: WalkBuilder) -> None:
    sql, params = _compile(builder(TAG_ID, 5))
    assert "DEPTH <" in sql, "recursive term must be bounded by depth"
    assert 5 in params.values(), "the configured bound must reach the query"


@pytest.mark.parametrize("builder", BUILDERS)
@pytest.mark.parametrize("depth", [0, -1, TAG_DEPTH_HARD_CEILING + 1])
def test_out_of_range_depth_is_rejected(builder: WalkBuilder, depth: int) -> None:
    with pytest.raises(UnboundedTraversalError):
        builder(TAG_ID, depth)


@pytest.mark.parametrize("builder", BUILDERS)
def test_hard_ceiling_is_accepted(builder: WalkBuilder) -> None:
    _, params = _compile(builder(TAG_ID, TAG_DEPTH_HARD_CEILING))
    assert TAG_DEPTH_HARD_CEILING in params.values()


def test_ancestors_and_descendants_walk_opposite_directions() -> None:
    up, _ = _compile(build_ancestors_stmt(TAG_ID, 3))
    down, _ = _compile(build_descendants_stmt(TAG_ID, 3))
    assert "TAG_ANCESTORS" in up
    assert "TAG_DESCENDANTS" in down
    assert up != down
