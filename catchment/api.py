"""FastAPI surface: webhook receivers and operational endpoints.

Polled sources (Substack RSS, IMAP, X bookmarks) run as RQ jobs and do not
appear here. Webhook handlers should enqueue and return quickly rather than
extracting inline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from catchment.classification.embeddings import close_embedder
from catchment.config import Settings, get_settings
from catchment.ingestion import whatsapp
from catchment.jobs.queue import close_pipeline_queue
from catchment.llm.tracing import flush_langfuse
from catchment.logging_config import configure_logging, get_logger, log_context
from catchment.storage.db import dispose_engine

logger = get_logger(__name__)


class HealthResponse(BaseModel):
    """Operational status. Deliberately free of configuration detail."""

    status: str
    version: str
    environment: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the process-wide connection pools.

    Shutdown runs in a ``finally`` so pools are released even when startup or
    serving raises. Containers get stopped and restarted routinely; leaking a
    Postgres pool per restart eventually exhausts the server's connection slots.
    """
    settings: Settings = get_settings()
    configure_logging(settings)
    logger.info("api starting", extra=log_context(environment=settings.env))
    try:
        yield
    finally:
        flush_langfuse()
        close_pipeline_queue()
        close_embedder()
        dispose_engine()
        logger.info("api stopping")


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    from catchment import __version__

    app = FastAPI(title="Catchment", version=__version__, lifespan=lifespan)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok", version=__version__, environment=get_settings().env
        )

    app.include_router(whatsapp.router)
    # /internal/* is deliberately NOT mounted here. It lives on
    # catchment.internal_app, bound to loopback, so the internet-facing surface
    # cannot serve it regardless of proxy configuration.
    return app


app = create_app()
