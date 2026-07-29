"""Groq provider.

Translates the router's provider-agnostic types into Groq's chat-completions
SDK and back. It does not trace, log content, or read configuration beyond its
own credentials — see ``catchment/llm/registry.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from catchment.config import Settings, get_settings
from catchment.llm.errors import LLMRateLimited, LLMResponseInvalid, LLMUnavailable
from catchment.llm.types import CompletionRequest, CompletionResult

PROVIDER_NAME: Final[str] = "groq"

#: Injectable so tests never import the SDK or touch the network.
ClientFactory = Callable[[str, float], Any]


def _default_client(api_key: str, timeout: float) -> Any:
    from groq import Groq

    return Groq(api_key=api_key, timeout=timeout)


class GroqProvider:
    """Chat completions via Groq."""

    name: str = PROVIDER_NAME

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client_factory: ClientFactory = _default_client,
    ) -> None:
        self._settings = settings or get_settings()
        self._client_factory = client_factory
        self._client: Any | None = None

    def _get_client(self) -> Any:
        """Build the SDK client lazily, so an unconfigured key fails at call
        time rather than at import time."""
        if self._client is None:
            key = self._settings.require_groq_key()
            self._client = self._client_factory(
                key.get_secret_value(), float(self._settings.llm_timeout_seconds)
            )
        return self._client

    def complete(self, request: CompletionRequest) -> CompletionResult:
        model = request.model or self._settings.llm_model
        max_tokens = request.max_tokens or self._settings.llm_max_tokens

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
        }
        if request.response_format == "json":
            # Groq exposes OpenAI-compatible JSON mode.
            payload["response_format"] = {"type": "json_object"}

        # Resolved outside the try: a missing API key is a configuration fault,
        # not a provider outage. Normalising it to LLMUnavailable would send a
        # permanently-failing request into retry logic.
        client = self._get_client()

        try:
            response = client.chat.completions.create(**payload)
        except Exception as error:  # noqa: BLE001 - normalised below
            raise _translate(error) from None

        return _to_result(response, model=model)


def _translate(error: Exception) -> LLMUnavailable:
    """Map an SDK exception onto the router's error types.

    The provider's own message is deliberately dropped: some vendors echo the
    submitted prompt back in error text, and that prompt carries ingested
    personal content.
    """
    name = type(error).__name__
    status = getattr(error, "status_code", None)

    if status == 429 or "RateLimit" in name:
        raise_as: type[LLMUnavailable] = LLMRateLimited
    else:
        raise_as = LLMUnavailable

    detail = f"status={status}" if status is not None else name
    return raise_as(f"groq request failed ({detail})")


def _to_result(response: Any, *, model: str) -> CompletionResult:
    """Read the pieces we need out of an OpenAI-shaped response."""
    try:
        text = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as error:
        raise LLMResponseInvalid("groq returned no completion choices") from error

    if not isinstance(text, str):
        raise LLMResponseInvalid("groq returned a non-text completion")

    usage = getattr(response, "usage", None)
    return CompletionResult(
        text=text,
        model=getattr(response, "model", model) or model,
        provider=PROVIDER_NAME,
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
    )


def build_groq_provider(settings: Settings | None = None) -> GroqProvider:
    """Factory registered under ``groq`` in the router."""
    return GroqProvider(settings)
