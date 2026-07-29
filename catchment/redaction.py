"""Pure helpers for keeping personal content and secrets out of logs.

This pipeline ingests private correspondence. The rule (CLAUDE.md) is that
message bodies, transcripts and email content never reach INFO-level logs;
identifiers and metadata do. These helpers are side-effect free — they always
return new values rather than mutating what they are given.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Final

#: Substrings that mark a field as a credential. Matched case-insensitively.
SECRET_FIELD_MARKERS: Final[tuple[str, ...]] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth_header",
    "cookie",
    "credential",
    "private_key",
    "session_id",
)

#: Substrings that mark a field as ingested personal content.
CONTENT_FIELD_MARKERS: Final[tuple[str, ...]] = (
    "body",
    "text",
    "content",
    "transcript",
    "caption",
    "snippet",
    "subject",
    "message",
    "html",
    "ocr",
    "raw",
)

REDACTED: Final[str] = "<redacted>"

_DIGEST_LENGTH: Final[int] = 12


def is_secret_field(name: str) -> bool:
    """Return True if a field name looks like it holds a credential."""
    lowered = name.lower()
    return any(marker in lowered for marker in SECRET_FIELD_MARKERS)


def is_content_field(name: str) -> bool:
    """Return True if a field name looks like it holds ingested personal content."""
    lowered = name.lower()
    return any(marker in lowered for marker in CONTENT_FIELD_MARKERS)


def content_digest(value: str) -> str:
    """Return a short, stable digest of ``value`` for correlating without exposing it."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]


def content_summary(value: object) -> str:
    """Describe content by shape only — length and digest, never the value itself.

    Safe to log at any level, and useful for spotting duplicate or empty
    extractions without reading anyone's mail.
    """
    if value is None:
        return "<content: none>"
    text = value if isinstance(value, str) else repr(value)
    return f"<content: {len(text)} chars sha256:{content_digest(text)}>"


def redact_value(name: str, value: Any, *, allow_content: bool = False) -> Any:
    """Return a log-safe replacement for a single field.

    Secrets are always masked. Content is summarised unless ``allow_content``
    is set, which callers should only do for DEBUG-level, non-production logs.
    """
    if is_secret_field(name):
        return REDACTED
    if is_content_field(name) and not allow_content:
        return content_summary(value)
    return value


def redact_mapping(
    mapping: Mapping[str, Any], *, allow_content: bool = False
) -> dict[str, Any]:
    """Return a new mapping with secret and content fields replaced.

    Nested mappings are redacted recursively. The input is never mutated.
    """
    redacted: dict[str, Any] = {}
    for key, value in mapping.items():
        if isinstance(value, Mapping):
            redacted[key] = redact_mapping(value, allow_content=allow_content)
        else:
            redacted[key] = redact_value(key, value, allow_content=allow_content)
    return redacted
