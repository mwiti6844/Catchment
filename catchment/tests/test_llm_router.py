"""The LLM router: provider swapping, error normalisation, and forced tracing."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from catchment.config import MissingConfiguration, Settings
from catchment.llm import registry
from catchment.llm.errors import (
    LLMRateLimited,
    LLMResponseInvalid,
    LLMUnavailable,
    ProviderNotRegistered,
)
from catchment.llm.providers.groq import GroqProvider
from catchment.llm.tracing import TracedProvider
from catchment.llm.types import CompletionRequest, CompletionResult, LLMProvider, Message

BODY = "Private message body that must not reach a log line"
PROMPT = [Message(role="user", content=BODY)]


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #


class FakeProvider:
    name = "fake"

    def __init__(self, result: CompletionResult | None = None) -> None:
        self.calls: list[CompletionRequest] = []
        self._result = result or CompletionResult(
            text="ok", model="fake-1", provider="fake", input_tokens=11, output_tokens=3
        )

    def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        return self._result


class FakeGeneration:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)


class FakeLangfuse:
    """Mimics the langfuse 4.x surface the tracer actually uses."""

    def __init__(self) -> None:
        self.observations: list[dict[str, Any]] = []
        self.generation = FakeGeneration()

    def start_as_current_observation(self, **kwargs: Any) -> Any:
        self.observations.append(kwargs)
        generation = self.generation

        class _Scope:
            def __enter__(self) -> FakeGeneration:
                return generation

            def __exit__(self, *exc: Any) -> None:
                return None

        return _Scope()

    def get_current_trace_id(self) -> str:
        return "trace-abc123"


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    registry.reset_provider_cache()
    yield
    registry.reset_provider_cache()


# --------------------------------------------------------------------------- #
# Registry / swapping
# --------------------------------------------------------------------------- #


def test_groq_is_registered_by_default() -> None:
    assert "groq" in registry.registered_providers()


def test_unknown_provider_names_the_registered_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATCHMENT_LLM_PROVIDER", "definitely-not-real")

    with pytest.raises(ProviderNotRegistered) as caught:
        registry.build_provider(Settings())

    assert "definitely-not-real" in str(caught.value)
    assert "groq" in str(caught.value)


def test_a_new_provider_is_one_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swapping providers must not require touching the classifier."""
    fake = FakeProvider()
    registry.register_provider("fake", lambda _settings: fake)
    monkeypatch.setenv("CATCHMENT_LLM_PROVIDER", "fake")

    provider = registry.build_provider(Settings())

    assert provider.name == "fake"


def test_provider_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    registry.register_provider("fake", lambda _settings: FakeProvider())
    monkeypatch.setenv("CATCHMENT_LLM_PROVIDER", "fake")

    assert registry.get_provider() is registry.get_provider()


# --------------------------------------------------------------------------- #
# Tracing is structural, not conventional
# --------------------------------------------------------------------------- #


def test_router_never_hands_out_an_untraced_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLAUDE.md guarantee: every LLM call goes through Langfuse."""
    registry.register_provider("fake", lambda _settings: FakeProvider())
    monkeypatch.setenv("CATCHMENT_LLM_PROVIDER", "fake")

    assert isinstance(registry.build_provider(Settings()), TracedProvider)


def test_completion_is_recorded_as_a_generation() -> None:
    client = FakeLangfuse()
    inner = FakeProvider()

    TracedProvider(inner, client=client, settings=Settings()).complete(
        CompletionRequest(messages=PROMPT, model="m-1")
    )

    observation = client.observations[0]
    assert observation["as_type"] == "generation"
    assert observation["model"] == "m-1"
    assert observation["metadata"]["provider"] == "fake"
    assert client.generation.updates[0]["usage_details"] == {"input": 11, "output": 3}


def test_trace_id_is_returned_for_audit() -> None:
    """Tag assignments carry this so a decision traces back to its prompt."""
    result = TracedProvider(
        FakeProvider(), client=FakeLangfuse(), settings=Settings()
    ).complete(CompletionRequest(messages=PROMPT))

    assert result.trace_id == "trace-abc123"


def test_unconfigured_tracing_warns_but_still_serves(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Losing tracing must not take ingestion down — but must be visible."""
    provider = TracedProvider(FakeProvider(), client=None, settings=Settings())

    with caplog.at_level(logging.WARNING):
        result = provider.complete(CompletionRequest(messages=PROMPT))

    assert result.text == "ok"
    assert "not traced" in caplog.records[-1].getMessage()


def test_llm_logs_carry_no_prompt_content(caplog: pytest.LogCaptureFixture) -> None:
    provider = TracedProvider(FakeProvider(), client=FakeLangfuse(), settings=Settings())

    with caplog.at_level(logging.INFO):
        provider.complete(CompletionRequest(messages=PROMPT))

    emitted = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert BODY not in emitted
    assert "input_tokens" in emitted


def test_result_carrying_a_trace_does_not_mutate_the_original() -> None:
    original = CompletionResult(text="t", model="m", provider="p")

    updated = original.with_trace("trace-1")

    assert original.trace_id is None
    assert updated.trace_id == "trace-1"
    assert updated is not original


# --------------------------------------------------------------------------- #
# Groq provider
# --------------------------------------------------------------------------- #


class FakeGroqResponse:
    def __init__(self, text: str = "hello", model: str = "llama-3.3-70b-versatile") -> None:
        message = type("M", (), {"content": text})()
        self.choices = [type("C", (), {"message": message})()]
        self.model = model
        self.usage = type("U", (), {"prompt_tokens": 42, "completion_tokens": 7})()


class FakeGroqClient:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response or FakeGroqResponse()
        self.error = error
        self.payloads: list[dict[str, Any]] = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **payload: Any) -> Any:
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def groq_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("CATCHMENT_GROQ_API_KEY", "gsk-test-key-not-real")
    return Settings()


def build(client: FakeGroqClient, settings: Settings) -> GroqProvider:
    return GroqProvider(settings, client_factory=lambda _key, _timeout: client)


def test_groq_satisfies_the_provider_protocol(groq_settings: Settings) -> None:
    assert isinstance(build(FakeGroqClient(), groq_settings), LLMProvider)


def test_groq_translates_request_and_response(groq_settings: Settings) -> None:
    client = FakeGroqClient()

    result = build(client, groq_settings).complete(
        CompletionRequest(messages=[Message(role="user", content="hi")])
    )

    payload = client.payloads[0]
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["model"] == groq_settings.llm_model
    assert result.provider == "groq"
    assert result.input_tokens == 42
    assert result.output_tokens == 7


def test_json_mode_is_requested_when_asked(groq_settings: Settings) -> None:
    client = FakeGroqClient()

    build(client, groq_settings).complete(
        CompletionRequest(messages=PROMPT, response_format="json")
    )

    assert client.payloads[0]["response_format"] == {"type": "json_object"}


def test_per_request_model_overrides_config(groq_settings: Settings) -> None:
    client = FakeGroqClient()

    build(client, groq_settings).complete(
        CompletionRequest(messages=PROMPT, model="llama-3.1-8b-instant")
    )

    assert client.payloads[0]["model"] == "llama-3.1-8b-instant"


def test_missing_key_raises_missing_configuration(settings: Settings) -> None:
    """The autouse fixture leaves CATCHMENT_GROQ_API_KEY unset."""
    provider = GroqProvider(settings, client_factory=lambda _k, _t: FakeGroqClient())

    with pytest.raises(MissingConfiguration, match="GROQ_API_KEY"):
        provider.complete(CompletionRequest(messages=PROMPT))


def test_rate_limit_is_normalised(groq_settings: Settings) -> None:
    error = type("RateLimitError", (Exception,), {})()
    error.status_code = 429

    with pytest.raises(LLMRateLimited):
        build(FakeGroqClient(error=error), groq_settings).complete(
            CompletionRequest(messages=PROMPT)
        )


def test_provider_errors_do_not_echo_the_prompt(groq_settings: Settings) -> None:
    """Some vendors put the submitted prompt in their error text."""
    error = RuntimeError(f"invalid request for prompt: {BODY}")

    with pytest.raises(LLMUnavailable) as caught:
        build(FakeGroqClient(error=error), groq_settings).complete(
            CompletionRequest(messages=PROMPT)
        )

    assert BODY not in str(caught.value)


@pytest.mark.parametrize(
    "response",
    [
        type("Empty", (), {"choices": []})(),
        type("NoText", (), {"choices": [type("C", (), {"message": None})()]})(),
    ],
)
def test_malformed_responses_raise(groq_settings: Settings, response: Any) -> None:
    with pytest.raises(LLMResponseInvalid):
        build(FakeGroqClient(response=response), groq_settings).complete(
            CompletionRequest(messages=PROMPT)
        )
