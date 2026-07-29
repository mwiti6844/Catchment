"""Provider-agnostic LLM access.

Shared by ``classification/`` and ``agents/``, which is why it sits at the top
level rather than inside either. Tracing is applied by the router, so every
call is a Langfuse generation regardless of which provider is configured.
"""

from catchment.llm.errors import (
    LLMError,
    LLMRateLimited,
    LLMResponseInvalid,
    LLMUnavailable,
    ProviderNotRegistered,
)
from catchment.llm.registry import (
    build_provider,
    get_provider,
    register_provider,
    registered_providers,
    reset_provider_cache,
)
from catchment.llm.types import (
    CompletionRequest,
    CompletionResult,
    LLMProvider,
    Message,
)

__all__ = [
    "CompletionRequest",
    "CompletionResult",
    "LLMError",
    "LLMProvider",
    "LLMRateLimited",
    "LLMResponseInvalid",
    "LLMUnavailable",
    "Message",
    "ProviderNotRegistered",
    "build_provider",
    "get_provider",
    "register_provider",
    "registered_providers",
    "reset_provider_cache",
]
