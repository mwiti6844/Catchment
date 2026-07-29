"""BGE-M3 embedding service.

Deliberately a separate deployable: FlagEmbedding pulls ~3GB of torch wheels,
and the API and worker images have no other use for them. Keeping it here means
a code change to the pipeline does not rebuild against those layers.

This service is *internal* — it has no authentication and must not be exposed
outside the compose network. It also never logs the text it embeds.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

MODEL_NAME = os.environ.get("EMBEDDER_MODEL", "BAAI/bge-m3")
EMBEDDING_DIM = int(os.environ.get("EMBEDDER_DIM", "1024"))
MAX_BATCH = int(os.environ.get("EMBEDDER_MAX_BATCH", "32"))

logging.basicConfig(
    level=os.environ.get("EMBEDDER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
)
logger = logging.getLogger("embedder")

_model: Any = None


def load_model() -> Any:
    """Load BGE-M3 once. First call downloads ~2GB into the model cache."""
    global _model

    if _model is None:
        from FlagEmbedding import BGEM3FlagModel

        logger.info("loading model %s", MODEL_NAME)
        _model = BGEM3FlagModel(MODEL_NAME, use_fp16=False)
        logger.info("model ready")
    return _model


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1)


class EmbedResponse(BaseModel):
    """Vectors only — the request text is never echoed back."""

    model: str
    dim: int
    vectors: list[list[float]]


class HealthResponse(BaseModel):
    status: str
    model: str
    dim: int
    loaded: bool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Load eagerly so the first real request doesn't pay the download, and so
    # an unhealthy container is visible at startup rather than at first use.
    load_model()
    yield


app = FastAPI(title="Catchment embedder", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok", model=MODEL_NAME, dim=EMBEDDING_DIM, loaded=_model is not None
    )


@app.post("/embed", response_model=EmbedResponse)
def embed(request: EmbedRequest) -> EmbedResponse:
    if len(request.texts) > MAX_BATCH:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"batch of {len(request.texts)} exceeds limit {MAX_BATCH}",
        )

    dense = load_model().encode(list(request.texts))["dense_vecs"]
    vectors = [[float(value) for value in vector] for vector in dense]

    for vector in vectors:
        if len(vector) != EMBEDDING_DIM:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"model returned dim {len(vector)}, expected {EMBEDDING_DIM}",
            )

    # Digest, not content — enough to correlate a vector with its input.
    logger.info(
        "embedded batch: count=%d digest=%s",
        len(vectors),
        hashlib.sha256("".join(request.texts).encode()).hexdigest()[:12],
    )
    return EmbedResponse(model=MODEL_NAME, dim=EMBEDDING_DIM, vectors=vectors)
