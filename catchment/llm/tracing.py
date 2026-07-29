"""Langfuse tracing, applied as a wrapper around any provider.

CLAUDE.md requires that *every* LLM call goes through Langfuse. Implementing
that inside each provider would make it a convention a new provider could
forget. Instead the router wraps whatever provider it builds in
:class:`TracedProvider`, so the guarantee is structural: an untraced provider
is not reachable through :func:`catchment.llm.registry.get_provider`.

**SDK and server versions are coupled.** This module targets the Langfuse v2
SDK, which posts to ``/api/public/ingestion``, matching the
``langfuse/langfuse:2`` server in docker-compose (Postgres only). The v3+ SDK
posts OTLP to ``/api/public/otel/v1/traces``, which a v2 server does not
expose — and the failure is silent: ``auth_check()`` still passes because it
hits a different endpoint, so calls look traced while nothing is recorded.
If you upgrade one, upgrade both, and re-run the integration check that a
trace actually lands.

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

        trace = client.trace(
            name=f"{self._provider.name}.complete",
            metadata={**request.metadata, "provider": self._provider.name},
        )
        generation = trace.generation(
            name="completion",
            model=model,
            input=[{"role": m.role, "content": m.content} for m in request.messages],
            model_parameters={
                "max_tokens": request.max_tokens or self._settings.llm_max_tokens,
                "response_format": request.response_format,
            },
        )

        try:
            result = self._provider.complete(request)
        except Exception as error:
            generation.end(level="ERROR", status_message=type(error).__name__)
            raise

        generation.end(output=result.text, usage=_usage(result))

        logger.info(
            "llm call complete",
            extra=log_context(
                provider=result.provider,
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                trace_id=trace.id,
            ),
        )
        return result.with_trace(trace.id)

    def _resolve_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        return get_langfuse_client(self._settings)


def _usage(result: CompletionResult) -> dict[str, Any]:
    """Langfuse v2 usage shape."""
    usage: dict[str, Any] = {"unit": "TOKENS"}
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

    client = Langfuse(
        public_key=resolved.langfuse_public_key.get_secret_value(),
        secret_key=resolved.langfuse_secret_key.get_secret_value(),
        host=resolved.langfuse_host,
    )

    # Verify once, at construction. Ingestion is fire-and-forget: a wrong key
    # or the wrong host returns 401 on a background thread and the SDK drops
    # the batch without raising, so calls look traced while nothing is
    # recorded. Keys generated on Langfuse Cloud against a self-hosted host
    # fail exactly this way. Checking here converts a silent gap in the audit
    # trail into one loud line at startup.
    if not _auth_ok(client, resolved.langfuse_host):
        return None
    return client


def _auth_ok(client: Any, host: str) -> bool:
    try:
        if client.auth_check():
            return True
    except Exception as error:  # noqa: BLE001 - never take ingestion down
        logger.error(
            "langfuse auth check failed; LLM calls will NOT be traced",
            extra=log_context(langfuse_host=host, error=type(error).__name__),
        )
        return False

    logger.error(
        "langfuse rejected the configured credentials; LLM calls will NOT be "
        "traced. Check the keys were generated on this host",
        extra=log_context(langfuse_host=host),
    )
    return False


_client: Any | None = None


def get_langfuse_client(settings: Settings | None = None) -> Any | None:
    """Return the process-wide client, creating it on first use.

    Shared because the SDK batches events on a background thread; a new client
    per call would spawn a thread per completion and drop anything not yet
    flushed when it was garbage-collected.
    """
    global _client

    if _client is None:
        _client = build_langfuse_client(settings)
    return _client


def flush_langfuse() -> None:
    """Send anything still buffered. Called on shutdown.

    The SDK batches, so a container that stops without flushing loses the
    trailing batch — which is exactly the traces for the work it did last.
    """
    global _client

    if _client is not None:
        try:
            _client.flush()
        finally:
            _client = None
