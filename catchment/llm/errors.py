"""Failure modes shared by every LLM provider.

Providers translate their SDK's exceptions into these so callers can handle a
rate limit the same way regardless of who is behind the router.
"""

from __future__ import annotations


class LLMError(RuntimeError):
    """Base class for LLM router failures."""


class ProviderNotRegistered(LLMError):
    """Raised when configuration names a provider nobody registered."""


class LLMUnavailable(LLMError):
    """Raised when the provider could not be reached or refused the request.

    Provider messages are not passed through verbatim — some vendors echo the
    submitted prompt (and therefore ingested content) back in error text.
    """


class LLMRateLimited(LLMUnavailable):
    """Raised when the provider rejected the request for rate limiting."""


class LLMResponseInvalid(LLMError):
    """Raised when a response could not be parsed into the expected shape."""
