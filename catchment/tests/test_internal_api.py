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

from catchment.internal_api import QueueCounts, read_queue_counts
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
def applied() -> list[uuid.UUID]:
    """Proposal ids the endpoint asked to execute."""
    return []


@pytest.fixture
def client(
    proposals: FakeProposals,
    applied: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
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
    # Executing the merge needs a real session and a real graph; that path is
    # covered in test_taxonomy_apply_integration.py. Here we only assert the
    # endpoint reaches for it, and only after an approval.
    monkeypatch.setattr("catchment.internal_api.apply_proposal", _record_apply(applied))
    yield TestClient(create_internal_app())


def _record_apply(applied: list[uuid.UUID]) -> Any:
    def _apply(proposal_id: uuid.UUID, *, session: Any) -> Any:
        applied.append(proposal_id)
        return SimpleNamespace(stats=SimpleNamespace(assignments_moved=3))

    return _apply


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


def test_every_internal_route_refuses_an_unauthenticated_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future route added without the dependency would be publicly open.

    Asserted by *calling* every route rather than by reading its dependency
    list. The earlier version inspected ``router.routes``, which under FastAPI's
    lazy router inclusion holds an opaque placeholder for a nested router rather
    than its routes — so an entire sub-router could be added and the check would
    pass without ever having looked at it. Driving the app instead tests what
    actually matters, and cannot be fooled by how the routers are assembled.
    """
    monkeypatch.setenv("CATCHMENT_INTERNAL_API_TOKEN", TOKEN)
    app = create_internal_app()
    unauthenticated = TestClient(app)

    checked = 0
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/internal"):
            continue
        for method in operations:
            # Path params are filled with a syntactically valid value: an
            # invalid one would 422 before the dependency ever ran, and the
            # route would look protected when it is not.
            url = path.replace("{proposal_id}", str(PROPOSAL_ID))
            url = url.replace("{item_id}", str(uuid.uuid4()))
            url = url.replace("{tag_id}", str(uuid.uuid4()))

            response = unauthenticated.request(method.upper(), url, json={})
            assert response.status_code == 403, f"{method.upper()} {path} is open"
            checked += 1

    assert checked >= 8, "the route sweep found suspiciously few routes"


# --------------------------------------------------------------------------- #
# Proposal decisions
# --------------------------------------------------------------------------- #


def test_approve_goes_through_the_repository(
    client: TestClient, proposals: FakeProposals, applied: list[uuid.UUID]
) -> None:
    response = decide(client)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["reviewed_by"] == "david"
    assert proposals.calls == [("approved", "david")]
    assert applied == [PROPOSAL_ID], "approval must execute the merge"
    assert body["assignments_moved"] == 3


def test_reject_goes_through_the_repository(
    client: TestClient, proposals: FakeProposals, applied: list[uuid.UUID]
) -> None:
    response = decide(client, decision="reject")

    assert response.json()["status"] == "rejected"
    assert proposals.calls == [("rejected", "david")]
    assert applied == [], "a rejection must never touch the graph"
    assert response.json()["assignments_moved"] is None


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


# --------------------------------------------------------------------------- #
# Tag graph and insights
# --------------------------------------------------------------------------- #


@pytest.fixture
def graph_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client whose graph repository is a stand-in.

    The query behaviour itself is covered against a real database in
    test_tag_graph_integration.py; what is asserted here is the translation
    layer — the status codes, and that a missing tag is a 404 rather than an
    empty graph.
    """
    monkeypatch.setenv("CATCHMENT_INTERNAL_API_TOKEN", TOKEN)

    class _Scope:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(
        "catchment.internal_graph_api.session_scope", lambda *a, **k: _Scope()
    )
    # One instance across requests: a fresh fake per call would mint a new tag
    # id each time, so the id handed out by /tags would never match the one the
    # graph route is asked for.
    fake = FakeGraph()
    monkeypatch.setattr(
        "catchment.internal_graph_api.TagGraphRepository", lambda _s: fake
    )
    yield TestClient(create_internal_app())


class FakeGraph:
    """Mirrors the repository's contract, including returning None for a
    missing tag rather than an empty neighbourhood."""

    def __init__(self) -> None:
        self.known = uuid.uuid4()

    def _node(self, level: int, slug: str) -> Any:
        return SimpleNamespace(
            tag_id=self.known if level == 0 else uuid.uuid4(),
            slug=slug,
            label=slug.title(),
            status="active",
            origin="llm",
            item_count=3,
            level=level,
        )

    def list_tags(self, *, limit: int = 200) -> list[Any]:
        return [
            SimpleNamespace(
                tag_id=self.known,
                slug="hydrology",
                label="Hydrology",
                status="active",
                origin="llm",
                item_count=3,
                parent_count=0,
                child_count=1,
            )
        ][:limit]

    def neighbourhood(self, tag_id: uuid.UUID, *, depth: int = 2) -> Any:
        if tag_id != self.known:
            return None
        root = self._node(0, "hydrology")
        return SimpleNamespace(
            root=root,
            depth=depth,
            nodes=[root, self._node(1, "drainage-basin")],
            edges=[],
            truncated=False,
        )


def test_an_unknown_tag_is_a_404_not_an_empty_graph(graph_client: TestClient) -> None:
    """An empty graph reads as 'this tag is isolated', which is a different and
    much more interesting claim than 'this tag does not exist'."""
    response = graph_client.get(
        f"/internal/tags/{uuid.uuid4()}/graph", headers={"X-Internal-Token": TOKEN}
    )
    assert response.status_code == 404


def test_the_graph_route_refuses_a_depth_past_the_bound(
    graph_client: TestClient,
) -> None:
    """CLAUDE.md bounds recursive walks. The route must not accept a depth it
    would then have to silently clamp."""
    response = graph_client.get(
        f"/internal/tags/{uuid.uuid4()}/graph?depth=99",
        headers={"X-Internal-Token": TOKEN},
    )
    assert response.status_code == 422


def test_the_graph_carries_signed_levels(graph_client: TestClient) -> None:
    """The sign is the direction, and it is what lets the client lay tags out
    in columns without re-deriving the hierarchy from the edge list."""
    client = graph_client
    known = str(
        [row["id"] for row in client.get(
            "/internal/tags", headers={"X-Internal-Token": TOKEN}
        ).json()][0]
    )

    body = client.get(
        f"/internal/tags/{known}/graph", headers={"X-Internal-Token": TOKEN}
    ).json()

    assert {node["level"] for node in body["nodes"]} == {0, 1}
    assert body["root"]["level"] == 0


def test_the_graph_response_carries_no_item_text() -> None:
    """The explorer draws structure. Item content belongs to the inbox."""
    from catchment.internal_graph_api import TagNodeView

    assert not {"text", "preview", "author", "url"} & set(TagNodeView.model_fields)


def test_the_insights_response_carries_no_item_text() -> None:
    """Insights counts. It links to items by id; it does not restate them."""
    from catchment.internal_insights_api import TagTrendView

    fields = set(TagTrendView.model_fields)
    assert not {"text", "title", "author", "preview", "samples"} & fields
    assert "sample_item_ids" in fields, "counts must remain traceable to items"


def test_insights_reports_the_window_it_used() -> None:
    """A trend without a stated window is unfalsifiable by construction."""
    from catchment.internal_insights_api import TrendReportView

    assert {"window_start", "window_end", "prior_start"} <= set(
        TrendReportView.model_fields
    )
