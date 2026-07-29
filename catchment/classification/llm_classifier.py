"""The real classifier: an LLM behind the router, prompted with candidate tags.

Implements the :class:`~catchment.classification.types.Classifier` protocol, so
the placeholder in ``classification/placeholder.py`` and this share one seam.
"""

from __future__ import annotations

from catchment.classification.prompt import (
    ClassificationParseError,
    build_messages,
    parse_response,
)
from catchment.classification.types import ClassificationResult
from catchment.llm.registry import get_provider
from catchment.llm.types import CompletionRequest, LLMProvider
from catchment.logging_config import get_logger, log_context

logger = get_logger(__name__)


class LLMClassifier:
    """Assigns tags by asking a model, constrained by the candidate tag list."""

    def __init__(self, provider: LLMProvider | None = None) -> None:
        # Resolved lazily so constructing a classifier never builds a provider
        # (and therefore never requires an API key) until it is actually used.
        self._provider = provider

    def _get_provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = get_provider()
        return self._provider

    def classify(self, text: str, *, known_tags: list[str]) -> ClassificationResult:
        """Return tag suggestions for ``text``.

        Raises :class:`ClassificationParseError` when the model's response is
        unusable — the caller decides whether to retry or fall back, since
        silently returning no tags would look like "this item has no topics".
        """
        provider = self._get_provider()
        result = provider.complete(
            CompletionRequest(
                messages=build_messages(text, known_tags=known_tags),
                response_format="json",
                metadata={"task": "tag_classification", "candidates": len(known_tags)},
            )
        )

        try:
            suggestions = parse_response(result.text, known_tags=known_tags)
        except ClassificationParseError:
            logger.warning(
                "classifier response unusable",
                extra=log_context(
                    model=result.model,
                    provider=result.provider,
                    trace_id=result.trace_id,
                    chars=len(result.text),
                ),
            )
            raise

        logger.info(
            "classified item",
            extra=log_context(
                model=result.model,
                suggestions=len(suggestions),
                coined=sum(1 for s in suggestions if s.is_new),
                trace_id=result.trace_id,
            ),
        )
        return ClassificationResult(
            suggestions=suggestions, model=result.model, trace_id=result.trace_id
        )
