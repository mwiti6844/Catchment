"""The HNSW index and ``ItemRepository.nearest`` must agree on their operator.

``nearest()`` orders by ``<=>`` (cosine), and ``ix_embeddings_vector_cosine`` is
built with ``vector_cosine_ops``. If either side changes independently nothing
raises — the planner just stops being able to use the index and the query
degrades to a sequential scan over every embedding. These tests turn that silent
failure into a loud one.

Two things make the assertion meaningful rather than accidental:

* ``SET LOCAL enable_seqscan = off`` removes the planner's cost-model
  preference, so what is asserted is *operator-class compatibility* — can this
  index serve this operator at all? — rather than a costing decision that a few
  hundred rows would decide the wrong way regardless.
* the statement under test is captured from ``ItemRepository.nearest`` itself
  rather than hand-written here, so changing the distance operator in the
  repository breaks these tests instead of quietly bypassing them.
"""

from __future__ import annotations

import math
import random
import re
import uuid
from pathlib import Path
from typing import Any, Final, cast

import pytest
from sqlalchemy import Executable, Select, insert, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex, Index
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.expression import ClauseElement

from catchment.storage.models import EMBEDDING_DIM, Embedding, Item
from catchment.storage.repositories import ItemRepository

pytestmark = pytest.mark.integration

INDEX_NAME: Final[str] = "ix_embeddings_vector_cosine"

#: Enough rows that an index scan is a plausible plan at all. The seqscan
#: setting below is what actually forces the planner's hand; this just keeps the
#: table from being degenerately small.
ROW_COUNT: Final[int] = 320

#: Fixed so a failing plan is reproducible rather than a one-off.
RANDOM_SEED: Final[int] = 20260729

MIGRATION_0001: Final[Path] = (
    Path(__file__).resolve().parents[1]
    / "storage"
    / "migrations"
    / "versions"
    / "0001_initial_schema.py"
)


# --------------------------------------------------------------------------- #
# EXPLAIN
# --------------------------------------------------------------------------- #


class Explain(Executable, ClauseElement):
    """``EXPLAIN <statement>``, compiled by SQLAlchemy so bound parameters (the
    query vector in particular) keep their type handling."""

    inherit_cache = False

    def __init__(self, statement: ClauseElement) -> None:
        self.statement = statement


@compiles(Explain, "postgresql")
def _compile_explain(element: Explain, compiler: SQLCompiler, **kw: Any) -> str:
    return "EXPLAIN " + compiler.process(element.statement, **kw)


def _explain(session: Session, statement: ClauseElement) -> str:
    """Return the planner's chosen plan for ``statement`` as text."""
    rows = session.execute(Explain(statement)).all()
    return "\n".join(str(row[0]) for row in rows)


class _StatementCapture:
    """Stands in for a ``Session`` so the statement a repository *actually*
    builds can be inspected, without changing the repository to expose it.

    ``ItemRepository`` only ever calls ``execute`` on its session, so delegating
    that one method is enough.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self.captured: ClauseElement | None = None

    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        self.captured = statement
        return self._session.execute(statement, *args, **kwargs)


def _nearest_statement(session: Session, vector: list[float]) -> ClauseElement:
    """The exact statement ``ItemRepository.nearest`` issues for ``vector``."""
    capture = _StatementCapture(session)
    ItemRepository(cast(Session, capture)).nearest(vector=vector, limit=10)
    assert capture.captured is not None, "nearest() issued no statement"
    return capture.captured


def _l2_equivalent(vector: list[float]) -> Select[Any]:
    """The same shape of query as ``nearest()`` but ordered by L2 (``<->``).

    The cosine index cannot serve this operator, which is precisely the point:
    it is the control that proves the positive assertion has teeth.
    """
    distance = Embedding.vector.l2_distance(vector).label("distance")
    return (
        select(Item, distance)
        .join(Embedding, Embedding.item_id == Item.id)
        .order_by(distance)
        .limit(10)
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _unit_vector(rng: random.Random) -> list[float]:
    raw = [rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIM)]
    norm = math.sqrt(sum(value * value for value in raw)) or 1.0
    return [value / norm for value in raw]


@pytest.fixture
def query_vector() -> list[float]:
    return _unit_vector(random.Random(RANDOM_SEED - 1))


@pytest.fixture
def populated_session(db_session: Session) -> Session:
    """A session holding ``ROW_COUNT`` embeddings, with seqscans disabled.

    ``SET LOCAL`` is scoped to the surrounding transaction, which the
    ``db_session`` fixture rolls back — nothing leaks into other tests.
    """
    rng = random.Random(RANDOM_SEED)
    items = [
        {
            "id": uuid.uuid4(),
            "source": "x",
            "source_id": f"vector-index-{index}",
            "kind": "link",
            "meta": {},
        }
        for index in range(ROW_COUNT)
    ]
    embeddings = [
        {
            "id": uuid.uuid4(),
            "item_id": item["id"],
            "model": "bge-m3",
            "dim": EMBEDDING_DIM,
            "vector": _unit_vector(rng),
        }
        for item in items
    ]

    db_session.execute(insert(Item), items)
    db_session.execute(insert(Embedding), embeddings)
    db_session.flush()

    # The key move: with sequential scans off, a plan that still refuses the
    # index is refusing it because the operator class does not match, not
    # because a small table made a seqscan cheaper.
    db_session.execute(text("SET LOCAL enable_seqscan = off"))
    return db_session


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def _normalise_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def test_declared_index_ddl_matches_migration_0001() -> None:
    """``create_all`` and the applied migration must build the same index.

    Runs without a database: it compares the DDL SQLAlchemy would emit for the
    declaration in ``models.py`` against the raw SQL in migration 0001.
    """
    index = next(
        candidate
        for candidate in cast(Any, Embedding.__table__).indexes
        if candidate.name == INDEX_NAME
    )
    declared = _normalise_sql(
        str(
            CreateIndex(cast(Index, index)).compile(
                dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
            )
        )
    )

    assert declared == (
        f"CREATE INDEX {INDEX_NAME} ON embeddings "
        "USING hnsw (vector vector_cosine_ops)"
    )

    # The migration builds it with raw SQL split over adjacent string literals;
    # dropping quotes and collapsing whitespace reunites them.
    migration_source = _normalise_sql(
        MIGRATION_0001.read_text(encoding="utf-8").replace('"', "")
    )
    assert declared in migration_source


def test_nearest_query_uses_the_cosine_index(
    populated_session: Session, query_vector: list[float]
) -> None:
    """The planner can serve ``nearest()``'s ordering from the HNSW index."""
    statement = _nearest_statement(populated_session, query_vector)

    plan = _explain(populated_session, statement)

    assert INDEX_NAME in plan, f"nearest() no longer uses {INDEX_NAME}:\n{plan}"


def test_a_different_distance_operator_cannot_use_the_cosine_index(
    populated_session: Session, query_vector: list[float]
) -> None:
    """Negative control: an L2 ordering must not reach the cosine index.

    If this ever passes *and* the test above fails, the index and the repository
    have swapped operators together. If both use the same index, the assertion
    above is vacuous.
    """
    plan = _explain(populated_session, _l2_equivalent(query_vector))

    assert INDEX_NAME not in plan, (
        f"an L2 ordering was served by {INDEX_NAME}; the operator-class "
        f"assertion above proves nothing:\n{plan}"
    )
