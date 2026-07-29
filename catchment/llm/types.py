"""Provider-agnostic types for the LLM router.

Nothing here mentions a vendor. A provider implementation translates these into
whatever its SDK wants and back again, which is what makes swapping providers a
registry change rather than a rewrite of the classifier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant"]

#: Providers that cannot honour a JSON request must say so rather than silently
#: returning prose the caller will fail to parse.
ResponseFormat = Literal["text", "json"]


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of a conversation."""

    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """What the caller wants. Provider- and model-agnostic."""

    messages: list[Message]
    #: Overrides the configured default when set — lets one call site use a
    #: cheaper model without reconfiguring the process.
    model: str | None = None
    max_tokens: int | None = None
    response_format: ResponseFormat = "text"
    #: Free-form labels attached to the trace. Never put content here.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """What came back, plus the provenance needed to audit the decision."""

    text: str
    model: str
    provider: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    #: Langfuse trace id. Carried onto ClassificationResult so any tag
    #: assignment can be traced back to the exact prompt that produced it.
    trace_id: str | None = None

    def with_trace(self, trace_id: str | None) -> CompletionResult:
        """Return a copy carrying a trace id. The original is unchanged."""
        return CompletionResult(
            text=self.text,
            model=self.model,
            provider=self.provider,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            trace_id=trace_id,
        )


@runtime_checkable
class LLMProvider(Protocol):
    """A single vendor's chat-completion surface.

    Implementations do exactly one thing: translate a
    :class:`CompletionRequest` into their SDK's call and the response back into
    a :class:`CompletionResult`. They do **not** trace, retry across providers,
    or read configuration beyond their own credentials — the router owns that,
    so those behaviours cannot be forgotten by a new provider.
    """

    name: str

    def complete(self, request: CompletionRequest) -> CompletionResult:
        """Run one completion."""
        ...
