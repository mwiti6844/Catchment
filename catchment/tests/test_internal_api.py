"""Internal admin routes: auth, proposal decisions, and queue counts.

These are reachable on the same app Caddy proxies to the internet, so the auth
tests matter as much as the behaviour ones.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from catchment.internal_api import QueueCounts, read_queue_counts, require_internal_token
from catchment.internal_app import create_internal_app

TOKEN = "internal-token-for-tests"
PROPOSAL_ID = uuid.uuid4()


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #


class FakeProposals:
    """Mirrors the repository's compare-and-swap semantics."""

    def __init__(self, *, pending: bool = True) -> None:
        self.pending = pending
        self.calls: list[tuple[str, str]] = []

    def _decide(self, proposal_id: uuid.UUID, status: str, reviewer: str) -> Any:
        from catchment.storage.repositories import RepositoryError

        # Mirrors the real repository's guard, so the fake cannot pass where
        # the repository would raise.
        if not reviewer.strip():
            raise RepositoryError("a reviewer identity is required to decide a proposal")
        if not self.pending:
            raise RepositoryError(f"proposal {proposal_id} is not pending review")
        self.pending = False
        self.calls.append((status, reviewer))
        return SimpleNamespace(
            id=proposal_id,
            status=status,
            reviewed_by=reviewer,
            reviewed_at=datetime(2026, 7, 29, tzinfo=UTC),
        )

    def approve(self, proposal_id: uuid.UUID, *, reviewer: str) -> Any:
        return self._decide(proposal_id, "approved", reviewer)

    def reject(self, proposal_id: uuid.UUID, *, reviewer: str) -> Any:
        return self._decide(proposal_id, "rejected", reviewer)


class FakeRegistry:
    def __init__(self, count: int) -> None:
        self.count = count


class FakeQueue:
    name = "catchment"

    def __init__(self, *, pending: int = 0, oldest_age: float | None = None) -> None:
        self.count = pending
        self.started_job_registry = FakeRegistry(1)
        self.finished_job_registry = FakeRegistry(12)
        self.failed_job_registry = FakeRegistry(2)
        self.deferred_job_registry = FakeRegistry(0)
        self.scheduled_job_registry = FakeRegistry(0)
        self._oldest_age = oldest_age

    def get_jobs(self, start: int, end: int) -> list[Any]:
        if self._oldest_age is None:
            return []
        enqueued = datetime.now(UTC) - timedelta(seconds=self._oldest_age)
        return [SimpleNamespace(enqueued_at=enqueued)]


@pytest.fixture
def proposals() -> FakeProposals:
    return FakeProposals()


@pytest.fixture
def client(
    proposals: FakeProposals, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("CATCHMENT_INTERNAL_API_TOKEN", TOKEN)

    class _Scope:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr("catchment.internal_api.session_scope", lambda *a, **k: _Scope())
    monkeypatch.setattr(
        "catchment.internal_api.TaxonomyProposalRepository", lambda _s: proposals
    )
    yield TestClient(create_internal_app())


def decide(client: TestClient, **body: Any) -> Any:
    return client.post(
        f"/internal/proposals/{PROPOSAL_ID}/decision",
        json={"decision": "approve", "reviewer": "david", **body},
        headers={"X-Internal-Token": TOKEN},
    )


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #


def test_missing_token_is_forbidden(client: TestClient) -> None:
    """api is publicly proxied — an open approval route would defeat the gate."""
    response = client.post(
        f"/internal/proposals/{PROPOSAL_ID}/decision",
        json={"decision": "approve", "reviewer": "david"},
    )
    assert response.status_code == 403


def test_wrong_token_is_forbidden(client: TestClient) -> None:
    response = client.post(
        f"/internal/proposals/{PROPOSAL_ID}/decision",
        json={"decision": "approve", "reviewer": "david"},
        headers={"X-Internal-Token": "not-the-token"},
    )
    assert response.status_code == 403


def test_queue_requires_a_token(client: TestClient) -> None:
    assert client.get("/internal/queue").status_code == 403


def test_unconfigured_token_disables_the_routes_rather_than_opening_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: a half-configured deployment must not expose the gate."""
    monkeypatch.delenv("CATCHMENT_INTERNAL_API_TOKEN", raising=False)
    unconfigured = TestClient(create_internal_app())

    response = unconfigured.post(
        f"/internal/proposals/{PROPOSAL_ID}/decision",
        json={"decision": "approve", "reviewer": "david"},
        headers={"X-Internal-Token": "anything"},
    )
    assert response.status_code == 503


def test_token_check_is_a_dependency_on_every_internal_route() -> None:
    """A future route added without the dependency would be publicly open."""
    from catchment.internal_api import router

    for route in router.routes:
        dependencies = getattr(route, "dependencies", [])
        assert any(
            d.dependency is require_internal_token for d in dependencies
        ), f"{getattr(route, 'path', route)} is missing the token dependency"


# --------------------------------------------------------------------------- #
# Proposal decisions
# --------------------------------------------------------------------------- #


def test_approve_goes_through_the_repository(
    client: TestClient, proposals: FakeProposals
) -> None:
    response = decide(client)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["reviewed_by"] == "david"
    assert proposals.calls == [("approved", "david")]


def test_reject_goes_through_the_repository(
    client: TestClient, proposals: FakeProposals
) -> None:
    response = decide(client, decision="reject")

    assert response.json()["status"] == "rejected"
    assert proposals.calls == [("rejected", "david")]


def test_deciding_twice_is_a_conflict(client: TestClient) -> None:
    """The repository's compare-and-swap surfacing, not a check added here."""
    assert decide(client).status_code == 200

    second = decide(client)

    assert second.status_code == 409
    assert "not pending" in second.json()["detail"]


@pytest.mark.parametrize("reviewer", ["", "   ", "\t\n"])
def test_a_reviewer_identity_is_required(client: TestClient, reviewer: str) -> None:
    """ck_proposals_reviewer_recorded means decisions can never be anonymous.

    Rejected at the boundary as a 422 naming the field, rather than reaching
    the repository and coming back as a less specific 409.
    """
    assert decide(client, reviewer=reviewer).status_code == 422


def test_reviewer_is_stored_stripped(
    client: TestClient, proposals: FakeProposals
) -> None:
    decide(client, reviewer="  david  ")
    assert proposals.calls == [("approved", "david")]


def test_unknown_decision_is_rejected(client: TestClient) -> None:
    assert decide(client, decision="apply").status_code == 422


def test_applying_is_not_reachable_from_the_dashboard() -> None:
    """Only approve/reject exist; applying a merge stays a backend job."""
    from typing import get_args

    from catchment.internal_api import ProposalDecision

    allowed = get_args(ProposalDecision.model_fields["decision"].annotation)
    assert set(allowed) == {"approve", "reject"}


# --------------------------------------------------------------------------- #
# Queue counts
# --------------------------------------------------------------------------- #


def test_queue_counts_are_reported(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "catchment.jobs.queue.build_queue", lambda *a, **k: FakeQueue(pending=3)
    )

    body = client.get("/internal/queue", headers={"X-Internal-Token": TOKEN}).json()

    assert body["pending"] == 3
    assert body["failed"] == 2
    assert body["finished"] == 12
    assert body["queue"] == "catchment"


def test_queue_age_is_reported_when_something_is_waiting() -> None:
    counts = read_queue_counts(FakeQueue(pending=1, oldest_age=90.0))

    assert counts.oldest_pending_seconds is not None
    assert 85 < counts.oldest_pending_seconds < 120


def test_empty_queue_reports_no_age() -> None:
    assert read_queue_counts(FakeQueue()).oldest_pending_seconds is None


def test_redis_outage_degrades_rather_than_500s(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_a: Any, **_k: Any) -> None:
        raise ConnectionError("redis is down")

    monkeypatch.setattr("catchment.jobs.queue.build_queue", explode)

    response = client.get("/internal/queue", headers={"X-Internal-Token": TOKEN})

    assert response.status_code == 503


def test_queue_response_carries_no_job_payloads() -> None:
    """Job arguments carry message text; only counts may leave this route."""
    fields = set(QueueCounts.model_fields)
    assert not {"jobs", "args", "text", "payload", "description"} & fields


# --------------------------------------------------------------------------- #
# Search wrapper
# --------------------------------------------------------------------------- #


class FakeSearchResult:
    def __init__(self, hits: list[Any]) -> None:
        self.hits = hits
        self.seed_count = sum(1 for h in hits if h.route == "seed")
        self.expanded_count = sum(1 for h in hits if h.route == "expanded")
        self.tags_walked = 3


def hit(item_id: uuid.UUID, route: str = "seed") -> Any:
    return SimpleNamespace(
        item_id=item_id,
        score=0.9 if route == "seed" else 0.2,
        route=route,
        distance=0.1 if route == "seed" else None,
        graph_depth=None if route == "seed" else 1,
        matched_tags=None if route == "seed" else 2,
    )


@pytest.fixture
def search_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CATCHMENT_INTERNAL_API_TOKEN", TOKEN)
    item_id = uuid.uuid4()

    class _Scope:
        def __enter__(self) -> object:
            return SimpleNamespace(
                execute=lambda *a, **k: SimpleNamespace(
                    all=lambda: [],
                    scalars=lambda: SimpleNamespace(all=lambda: []),
                )
            )

        def __exit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr("catchment.internal_api.session_scope", lambda *a, **k: _Scope())
    monkeypatch.setattr("catchment.internal_api.ItemRepository", lambda _s: object())
    monkeypatch.setattr("catchment.internal_api.TagRepository", lambda _s: object())
    monkeypatch.setattr("catchment.internal_api.get_embedder", lambda *a, **k: object())
    monkeypatch.setattr(
        "catchment.retrieval.search", lambda *a, **k: FakeSearchResult([hit(item_id)])
    )
    yield TestClient(create_internal_app())


def test_search_requires_a_token(search_client: TestClient) -> None:
    assert search_client.get("/internal/search?q=hydrology").status_code == 403


def test_search_rejects_an_empty_query(search_client: TestClient) -> None:
    """Embedding whitespace would return whatever is nearest to nothing."""
    response = search_client.get(
        "/internal/search?q=", headers={"X-Internal-Token": TOKEN}
    )
    assert response.status_code == 422


def test_search_delegates_rather_than_reimplementing(
    search_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint must call retrieval, not do vector maths of its own."""
    called: list[str] = []

    def spy(query: str, **kwargs: Any) -> Any:
        called.append(query)
        return FakeSearchResult([])

    monkeypatch.setattr("catchment.retrieval.search", spy)

    response = search_client.get(
        "/internal/search?q=hydrology", headers={"X-Internal-Token": TOKEN}
    )

    assert response.status_code == 200
    assert called == ["hydrology"]


def test_search_reports_embedder_outage_as_503(
    search_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catchment.classification.embeddings import EmbeddingUnavailable

    def explode(*a: Any, **k: Any) -> Any:
        raise EmbeddingUnavailable("down")

    monkeypatch.setattr("catchment.retrieval.search", explode)

    response = search_client.get(
        "/internal/search?q=x", headers={"X-Internal-Token": TOKEN}
    )
    assert response.status_code == 503


def test_search_response_carries_no_item_text() -> None:
    """Search results are a list view — text belongs on Item detail only."""
    from catchment.internal_api import SearchHitView

    assert "text" not in SearchHitView.model_fields
    assert "preview_chars" in SearchHitView.model_fields


# --------------------------------------------------------------------------- #
# Connector health
# --------------------------------------------------------------------------- #


def test_staleness_thresholds_differ_by_cadence() -> None:
    """WhatsApp is webhook-driven and irregular; IMAP is polled."""
    from catchment.internal_api import STALE_AFTER

    assert STALE_AFTER["whatsapp"] > STALE_AFTER["email"]


def test_a_source_that_never_succeeded_is_stale() -> None:
    from datetime import UTC, datetime

    from catchment.internal_api import _is_stale

    row = SimpleNamespace(source="email", last_success_at=None)
    assert _is_stale(row, now=datetime.now(UTC)) is True


def test_a_recent_success_is_not_stale() -> None:
    from datetime import UTC, datetime, timedelta

    from catchment.internal_api import _is_stale

    now = datetime.now(UTC)
    row = SimpleNamespace(source="email", last_success_at=now - timedelta(minutes=5))
    assert _is_stale(row, now=now) is False


def test_an_old_success_is_stale() -> None:
    from datetime import UTC, datetime, timedelta

    from catchment.internal_api import _is_stale

    now = datetime.now(UTC)
    row = SimpleNamespace(source="email", last_success_at=now - timedelta(days=2))
    assert _is_stale(row, now=now) is True


# --------------------------------------------------------------------------- #
# Inbox status derivation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tags", "llm", "extraction", "failures", "expected"),
    [
        (2, 2, True, 0, "classified"),
        (1, 0, True, 1, "failed"),
        (1, 0, False, 0, "nothing to classify"),
        (1, 0, True, 0, "pending"),
    ],
)
def test_classification_status_distinguishes_the_three_failure_shapes(
    tags: int, llm: int, extraction: bool, failures: int, expected: str
) -> None:
    """The placeholder tag alone cannot tell these apart."""
    from catchment.internal_api import _classification_status

    assert (
        _classification_status(
            tag_count=tags,
            llm_tags=llm,
            has_extraction=extraction,
            open_failures=failures,
        )
        == expected
    )
