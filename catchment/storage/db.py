"""Engine and session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from catchment.config import Settings, get_settings
from catchment.logging_config import get_logger, log_context

logger = get_logger(__name__)

# An explicit singleton rather than an ``lru_cache``: the pool has to be
# disposed on shutdown, and a cache gives no way to reach the instance it holds
# without risking constructing a second one.
_engine: Engine | None = None


def get_engine(settings: Settings | None = None) -> Engine:
    """Return the process-wide engine, creating it on first use.

    ``pool_pre_ping`` keeps long-lived RQ workers from handing out connections
    the database has already closed.
    """
    global _engine

    if _engine is None:
        resolved = settings or get_settings()
        _engine = create_engine(
            str(resolved.database_url),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            future=True,
        )
    return _engine


def get_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Return a session factory bound to ``engine`` (or the default engine)."""
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Yield a transactional session, committing on success and rolling back on error."""
    session = get_session_factory(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        # Log the failure without the statement parameters, which may carry
        # ingested content.
        logger.exception("transaction rolled back", extra=log_context(bind=repr(session.bind)))
        raise
    finally:
        session.close()


def dispose_engine() -> None:
    """Close every pooled connection and drop the engine.

    Called on application shutdown. In a container that gets started and
    stopped routinely, leaving the pool open leaks server-side connections
    across restarts until Postgres refuses new ones.
    """
    global _engine

    if _engine is not None:
        _engine.dispose()
        _engine = None


def reset_engine_cache() -> None:
    """Drop the cached engine. Intended for tests only."""
    dispose_engine()
