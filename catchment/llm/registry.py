"""The router: configuration name -> traced provider.

Swapping providers is a one-line config change (``CATCHMENT_LLM_PROVIDER``)
plus a registration. Adding a provider never touches the classifier.

Everything handed out here is wrapped in
:class:`~catchment.llm.tracing.TracedProvider`, so a provider cannot reach a
caller untraced.
"""

from __future__ import annotations

from collections.abc import Callable

from catchment.config import Settings, get_settings
from catchment.llm.errors import ProviderNotRegistered
from catchment.llm.providers.groq import PROVIDER_NAME as GROQ
from catchment.llm.providers.groq import build_groq_provider
from catchment.llm.tracing import TracedProvider
from catchment.llm.types import LLMProvider

ProviderFactory = Callable[[Settings], LLMProvider]

_REGISTRY: dict[str, ProviderFactory] = {GROQ: build_groq_provider}


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Register a provider factory under ``name``.

    Call at import time from a provider module. Re-registering a name replaces
    it, which is what makes a test double easy to install.
    """
    _REGISTRY[name] = factory


def registered_providers() -> tuple[str, ...]:
    """Return every registered provider name, sorted."""
    return tuple(sorted(_REGISTRY))


def build_provider(settings: Settings | None = None) -> LLMProvider:
    """Build the configured provider, wrapped in tracing.

    Raises :class:`ProviderNotRegistered` naming the available providers, since
    the usual cause is a typo in ``CATCHMENT_LLM_PROVIDER``.
    """
    resolved = settings or get_settings()
    factory = _REGISTRY.get(resolved.llm_provider)
    if factory is None:
        raise ProviderNotRegistered(
            f"unknown LLM provider {resolved.llm_provider!r}; "
            f"registered: {', '.join(registered_providers()) or 'none'}"
        )
    return TracedProvider(factory(resolved), settings=resolved)


_provider: LLMProvider | None = None


def get_provider(settings: Settings | None = None) -> LLMProvider:
    """Return the process-wide provider, creating it on first use."""
    global _provider

    if _provider is None:
        _provider = build_provider(settings)
    return _provider


def reset_provider_cache() -> None:
    """Drop the cached provider. Intended for tests only."""
    global _provider

    _provider = None
