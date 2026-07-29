"""Client for the BGE-M3 embedding service.

The model itself runs in a separate container (``embedder/``). This module is
the thin HTTP client the worker uses, so the worker image stays free of torch.

Never logs the text being embedded.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

import httpx

from catchment.config import Settings, get_settings
from catchment.logging_config import get_logger, log_context
from catchment.storage.models import EMBEDDING_DIM

logger = get_logger(__name__)

Vector = list[float]


class EmbeddingError(RuntimeError):
    """Base class for embedding failures."""


class EmbeddingUnavailable(EmbeddingError):
    """Raised when the embedder could not be reached or returned an error."""


class EmbeddingInvalid(EmbeddingError):
    """Raised when the embedder returned a payload we cannot use."""


@runtime_checkable
class Embedder(Protocol):
    """Turns text into vectors."""

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        """Return one vector per input text, in order."""
        ...


class HttpEmbedder:
    """Calls the embedder service over HTTP."""

    def __init__(
        self, settings: Settings | None = None, client: httpx.Client | None = None
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self._settings.embedder_url.rstrip("/"),
                timeout=float(self._settings.embedder_timeout_seconds),
            )
        return self._client

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        if not texts:
            return []

        try:
            response = self._get_client().post("/embed", json={"texts": list(texts)})
            response.raise_for_status()
            payload: Any = response.json()
        except httpx.HTTPStatusError as error:
            # Status only — an embedder error body can quote the submitted text.
            raise EmbeddingUnavailable(
                f"embedder returned HTTP {error.response.status_code}"
            ) from None
        except httpx.HTTPError as error:
            raise EmbeddingUnavailable(
                f"cannot reach embedder: {type(error).__name__}"
            ) from None
        except ValueError:
            raise EmbeddingInvalid("embedder returned a non-JSON body") from None

        vectors = _validate(payload, expected=len(texts))
        logger.info(
            "embedded batch",
            extra=log_context(count=len(vectors), model=payload.get("model")),
        )
        return vectors

    def close(self) -> None:
        """Release the HTTP connection pool."""
        if self._client is not None:
            self._client.close()
            self._client = None


def _validate(payload: Any, *, expected: int) -> list[Vector]:
    """Check the response shape before it can poison the ``embeddings`` table.

    A wrong dimension would be rejected by pgvector at insert time, but with a
    far less obvious error than this one.
    """
    if not isinstance(payload, dict):
        raise EmbeddingInvalid("embedder response was not an object")

    vectors = payload.get("vectors")
    if not isinstance(vectors, list) or len(vectors) != expected:
        raise EmbeddingInvalid(
            f"embedder returned {len(vectors) if isinstance(vectors, list) else 0} "
            f"vectors for {expected} texts"
        )

    validated: list[Vector] = []
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != EMBEDDING_DIM:
            raise EmbeddingInvalid(
                f"embedder returned dim {len(vector) if isinstance(vector, list) else 0}, "
                f"expected {EMBEDDING_DIM}"
            )
        validated.append([float(value) for value in vector])
    return validated


_embedder: HttpEmbedder | None = None


def get_embedder(settings: Settings | None = None) -> HttpEmbedder:
    """Return the process-wide embedder client, creating it on first use."""
    global _embedder

    if _embedder is None:
        _embedder = HttpEmbedder(settings)
    return _embedder


def close_embedder() -> None:
    """Close and drop the cached client. Called on shutdown."""
    global _embedder

    if _embedder is not None:
        _embedder.close()
        _embedder = None
