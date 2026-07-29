"""The pipeline ingests private correspondence — these tests are the guardrail."""

from __future__ import annotations

import logging

import pytest

from catchment.logging_config import (
    ContextFormatter,
    RedactionFilter,
    configure_logging,
    log_context,
)
from catchment.redaction import (
    REDACTED,
    content_digest,
    content_summary,
    is_content_field,
    is_secret_field,
    redact_mapping,
    redact_value,
)

SECRET = "sk-live-do-not-leak"
BODY = "Hi, here is the confidential thing we discussed at dinner."


@pytest.mark.parametrize(
    "name",
    ["password", "api_key", "APIKEY", "webhook_secret", "access_token", "Cookie"],
)
def test_secret_fields_detected(name: str) -> None:
    assert is_secret_field(name)


@pytest.mark.parametrize(
    "name", ["body", "message_body", "transcript", "ocr_text", "email_subject"]
)
def test_content_fields_detected(name: str) -> None:
    assert is_content_field(name)


@pytest.mark.parametrize("name", ["item_id", "source", "created_at", "chars", "count"])
def test_metadata_fields_pass_through(name: str) -> None:
    assert not is_secret_field(name)
    assert not is_content_field(name)


def test_secrets_are_masked_even_at_debug() -> None:
    assert redact_value("api_key", SECRET, allow_content=True) == REDACTED


def test_content_summary_omits_the_content() -> None:
    summary = content_summary(BODY)
    assert BODY not in summary
    assert str(len(BODY)) in summary
    assert content_digest(BODY) in summary


def test_content_digest_is_stable_and_short() -> None:
    assert content_digest(BODY) == content_digest(BODY)
    assert content_digest(BODY) != content_digest(BODY + "!")
    assert len(content_digest(BODY)) == 12


def test_redact_mapping_does_not_mutate_input() -> None:
    original = {"item_id": "abc", "body": BODY, "token": SECRET}
    snapshot = dict(original)

    redacted = redact_mapping(original)

    assert original == snapshot
    assert redacted is not original
    assert redacted["item_id"] == "abc"
    assert redacted["token"] == REDACTED
    assert BODY not in redacted["body"]


def test_redact_mapping_recurses_into_nested_payloads() -> None:
    redacted = redact_mapping({"envelope": {"transcript": BODY, "secret": SECRET}})
    assert BODY not in redacted["envelope"]["transcript"]
    assert redacted["envelope"]["secret"] == REDACTED


def _emit(level: int, *, production: bool, **context: object) -> str:
    record = logging.LogRecord(
        name="catchment.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg="handled item",
        args=None,
        exc_info=None,
    )
    for key, value in context.items():
        setattr(record, key, value)

    RedactionFilter(allow_content_at_debug=not production).filter(record)
    return ContextFormatter("%(message)s").format(record)


def test_info_logs_never_carry_message_bodies() -> None:
    rendered = _emit(logging.INFO, production=False, item_id="i-1", body=BODY)
    assert BODY not in rendered
    assert "i-1" in rendered
    assert "chars" in rendered


def test_debug_logs_may_carry_content_outside_production() -> None:
    rendered = _emit(logging.DEBUG, production=False, transcript=BODY)
    assert BODY in rendered


def test_production_debug_logs_still_redact_content() -> None:
    rendered = _emit(logging.DEBUG, production=True, transcript=BODY)
    assert BODY not in rendered


def test_secrets_never_render_at_any_level() -> None:
    for level in (logging.DEBUG, logging.INFO, logging.ERROR):
        rendered = _emit(level, production=False, whatsapp_webhook_secret=SECRET)
        assert SECRET not in rendered


def test_configure_logging_installs_the_filter() -> None:
    configure_logging()
    handler = logging.getLogger().handlers[0]
    assert any(isinstance(f, RedactionFilter) for f in handler.filters)


def test_configure_logging_disables_uvicorn_access_log_query_strings() -> None:
    """Handshake verify tokens live in URLs that Uvicorn otherwise logs raw."""
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.disabled = False

    configure_logging()

    assert access_logger.disabled is True


def test_log_context_renames_reserved_record_attributes() -> None:
    context = log_context(created=1, name="connector", item_id="i-1")

    assert context["ctx_created"] == 1
    assert context["ctx_name"] == "connector"
    assert context["item_id"] == "i-1"


def test_configure_logging_is_idempotent() -> None:
    configure_logging()
    configure_logging()
    assert len(logging.getLogger().handlers) == 1
