"""The internal ASGI app — dashboard only, loopback only.

Separate from ``catchment.api`` on purpose. The public app no longer mounts
``/internal/*`` at all, so the internet-facing surface *cannot* serve these
routes; correctness no longer depends on a Caddy rule and a token check both
staying right. The token check remains as defence in depth.

This app is bound to 127.0.0.1 by compose and has no Caddy route. It returns
real WhatsApp and email content, which is exactly why it is never public.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from catchment.classification.embeddings import close_embedder
from catchment.config import Settings, get_settings
from catchment.internal_api import router as internal_router
from catchment.jobs.queue import close_pipeline_queue
from catchment.llm.tracing import flush_langfuse
from catchment.logging_config import configure_logging, get_logger, log_context
from catchment.storage.db import dispose_engine

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    configure_logging(settings)
    logger.info("internal api starting", extra=log_context(environment=settings.env))
    try:
        yield
    finally:
        flush_langfuse()
        close_pipeline_queue()
        close_embedder()
        dispose_engine()
        logger.info("internal api stopping")


def create_internal_app() -> FastAPI:
    """Build the internal-only application."""
    from catchment import __version__

    app = FastAPI(
        title="Catchment (internal)",
        version=__version__,
        lifespan=lifespan,
        # No public docs: this app is not a product surface.
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    # The dashboard is served from a Vite dev server on another loopback port
    # during development. Only loopback origins are allowed — this app is never
    # reachable from anywhere else, so a permissive origin would still be wrong.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(internal_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "surface": "internal"}

    return app


app = create_internal_app()


def main() -> int:
    """Entry point for ``catchment-internal-api``.

    Binds loopback by default. Overriding the host would expose personal
    content, so it is not read from configuration.
    """
    import os

    import uvicorn

    uvicorn.run(
        "catchment.internal_app:app",
        host="127.0.0.1",
        port=int(os.environ.get("INTERNAL_API_PORT", "8002")),
        access_log=False,
    )
    return 0
