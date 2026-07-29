from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from catchment.config import Settings, reset_settings_cache
from catchment.storage.db import reset_engine_cache
from catchment.storage.models import Base

_TEST_ENV = {
    "CATCHMENT_DATABASE_URL": "postgresql+psycopg://catchment:pw@localhost:5432/catchment_test",
    "CATCHMENT_REDIS_URL": "redis://localhost:6379/1",
    "CATCHMENT_ENV": "test",
    "CATCHMENT_LOG_LEVEL": "DEBUG",
    "CATCHMENT_WHATSAPP_WEBHOOK_SECRET": "test-app-secret",
    "CATCHMENT_WHATSAPP_VERIFY_TOKEN": "test-verify-token",
}

# Read at import time: the autouse fixture below strips CATCHMENT_* from the
# environment for each test, so these must be captured before that runs.
INTEGRATION_DB_URL = os.environ.get("CATCHMENT_TEST_DATABASE_URL")

#: Set in CI. Turns "no database configured" from a skip into a failure, so a
#: misconfigured pipeline cannot silently stop proving the schema guarantees.
REQUIRE_INTEGRATION = os.environ.get("CATCHMENT_REQUIRE_INTEGRATION") == "1"


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every test a clean, fully specified environment.

    ``Settings`` reads ``.env`` in normal operation. Tests must not: a
    developer's local file would otherwise supply values a test deliberately
    unset, quietly turning "this fails fast when unconfigured" into a pass.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    for key in list(os.environ):
        if key.startswith("CATCHMENT_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in _TEST_ENV.items():
        monkeypatch.setenv(key, value)

    reset_settings_cache()
    reset_engine_cache()
    yield
    reset_settings_cache()
    reset_engine_cache()


@pytest.fixture
def settings() -> Settings:
    return Settings()


# --------------------------------------------------------------------------- #
# Integration fixtures — shared by every module marked ``integration``
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    """A live Postgres with pgvector, or a skip (a failure under CI).

    The schema is built with ``create_all`` rather than by running Alembic, so
    a broken migration shows up as a migration test rather than as noise in
    every integration test.
    """
    if not INTEGRATION_DB_URL:
        if REQUIRE_INTEGRATION:
            pytest.fail(
                "CATCHMENT_REQUIRE_INTEGRATION=1 but CATCHMENT_TEST_DATABASE_URL is "
                "unset — integration tests must not be skipped in CI"
            )
        pytest.skip("set CATCHMENT_TEST_DATABASE_URL to run integration tests")

    engine = create_engine(INTEGRATION_DB_URL, future=True)
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    yield engine

    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    """A session inside a transaction that is rolled back after each test."""
    connection = db_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)

    yield session

    session.close()
    transaction.rollback()
    connection.close()
