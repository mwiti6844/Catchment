"""Langfuse tracing, applied as a wrapper around any provider.

CLAUDE.md requires that *every* LLM call goes through Langfuse. Implementing
that inside each provider would make it a convention a new provider could
forget. Instead the router wraps whatever provider it builds in
:class:`TracedProvider`, so the guarantee is structural: an untraced provider
is not reachable through :func:`catchment.llm.registry.get_provider`.

Prompts sent to Langfuse contain ingested content by design — that is the point
of tracing a classifier decision. Langfuse is self-hosted (CLAUDE.md), so that
content stays on infrastructure we control. Application logs, by contrast, get
counts and ids only.
"""

from __future__ import annotations

from typing import Any

from catchment.config import Settings, get_settings
from catchment.llm.types import CompletionRequest, CompletionResult, LLMProvider
from catchment.logging_config import get_logger, log_context

logger = get_logger(__name__)


class TracedProvider:
    """Wraps a provider so every completion produces a Langfuse generation."""

    def __init__(
        self,
        provider: LLMProvider,
        client: Any | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._provider = provider
        self._client = client
        self._settings = settings or get_settings()
        # Plain attribute, not a property: the LLMProvider protocol declares
        # `name` as a settable variable, which a read-only property does not
        # satisfy.
        self.name = provider.name

    def complete(self, request: CompletionRequest) -> CompletionResult:
        client = self._resolve_client()
        model = request.model or self._settings.llm_model

        if client is None:
            # Tracing unconfigured: still serve the call, but say so loudly —
            # a silently untraced classifier decision is unauditable.
            logger.warning(
                "llm call not traced: langfuse is unconfigured",
                extra=log_context(provider=self._provider.name, model=model),
            )
            return self._provider.complete(request)

        with client.start_as_current_observation(
            name=f"{self._provider.name}.complete",
            as_type="generation",
            input=[{"role": m.role, "content": m.content} for m in request.messages],
            model=model,
            model_parameters={
                "max_tokens": request.max_tokens or self._settings.llm_max_tokens,
                "response_format": request.response_format,
            },
            metadata={**request.metadata, "provider": self._provider.name},
        ) as generation:
            result = self._provider.complete(request)
            generation.update(
                output=result.text,
                usage_details=_usage(result),
            )
            trace_id = client.get_current_trace_id()

        logger.info(
            "llm call complete",
            extra=log_context(
                provider=result.provider,
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                trace_id=trace_id,
            ),
        )
        return result.with_trace(trace_id)

    def _resolve_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        return build_langfuse_client(self._settings)


def _usage(result: CompletionResult) -> dict[str, int]:
    usage: dict[str, int] = {}
    if result.input_tokens is not None:
        usage["input"] = result.input_tokens
    if result.output_tokens is not None:
        usage["output"] = result.output_tokens
    return usage


def build_langfuse_client(settings: Settings | None = None) -> Any | None:
    """Construct a Langfuse client, or None when tracing is unconfigured.

    Returns None rather than raising so a missing key degrades to an untraced
    call with a warning, instead of taking ingestion down.
    """
    resolved = settings or get_settings()
    if resolved.langfuse_public_key is None or resolved.langfuse_secret_key is None:
        return None

    from langfuse import Langfuse

    return Langfuse(
        public_key=resolved.langfuse_public_key.get_secret_value(),
        secret_key=resolved.langfuse_secret_key.get_secret_value(),
        host=resolved.langfuse_host,
        environment=resolved.env,
    )
