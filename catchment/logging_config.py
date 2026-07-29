"""Logging setup with redaction wired in at the handler level.

Redaction is enforced by a :class:`logging.Filter` rather than by convention,
so a careless ``logger.info("...", extra={"body": ...})`` still cannot emit
personal content.
"""

from __future__ import annotations

import logging
from typing import Any

from catchment.config import Settings, get_settings
from catchment.redaction import redact_value

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"

# Attributes the stdlib puts on every LogRecord; not caller-supplied context.
_RESERVED_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class RedactionFilter(logging.Filter):
    """Scrub secret and content fields from every record's ``extra`` context.

    Content is allowed through only at DEBUG level outside production, which
    is the one case where a developer is deliberately inspecting a payload on
    their own machine.
    """

    def __init__(self, *, allow_content_at_debug: bool = True) -> None:
        super().__init__()
        self._allow_content_at_debug = allow_content_at_debug

    def filter(self, record: logging.LogRecord) -> bool:
        allow_content = self._allow_content_at_debug and record.levelno <= logging.DEBUG
        for key, value in list(record.__dict__.items()):
            if key in _RESERVED_ATTRS:
                continue
            replacement = redact_value(key, value, allow_content=allow_content)
            if replacement is not value:
                setattr(record, key, replacement)
        return True


class ContextFormatter(logging.Formatter):
    """Append caller-supplied context as ``key=value`` pairs after the message."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        context = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_ATTRS
        }
        if not context:
            return base
        rendered = " ".join(f"{key}={value!r}" for key, value in sorted(context.items()))
        return f"{base} {rendered}"


def configure_logging(settings: Settings | None = None) -> None:
    """Install the redacting handler on the root logger. Idempotent."""
    resolved = settings or get_settings()
    allow_content_at_debug = not resolved.is_production

    handler = logging.StreamHandler()
    handler.setFormatter(ContextFormatter(_LOG_FORMAT))
    handler.addFilter(RedactionFilter(allow_content_at_debug=allow_content_at_debug))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved.log_level)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger. Prefer this over ``logging.getLogger`` directly."""
    return logging.getLogger(name)


def log_context(**fields: Any) -> dict[str, Any]:
    """Build an ``extra=`` payload for ``logger.*`` calls.

    Keys that collide with stdlib ``LogRecord`` attributes (``created``,
    ``name``, ``module``, …) are prefixed rather than allowed to raise at the
    call site — a log line must never be the thing that breaks ingestion.
    Values are redacted by :class:`RedactionFilter` on emit.
    """
    return {
        (f"ctx_{key}" if key in _RESERVED_ATTRS else key): value
        for key, value in fields.items()
    }
