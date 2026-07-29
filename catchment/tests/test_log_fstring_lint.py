"""Tests for the LOG001 rule in ``tools/check_log_fstrings.py``.

The rule exists because ``RedactionFilter`` only sees a record's ``extra=``
mapping. Anything formatted into the message string is flat text by the time
the filter runs, so it has to be caught statically instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.check_log_fstrings import (
    RULE_CODE,
    check_paths,
    check_source,
    is_sensitive_name,
    main,
)

REPO_PACKAGE = Path(__file__).resolve().parents[1]


def violations(source: str) -> list[str]:
    return [v.name for v in check_source(source, Path("example.py"))]


# --------------------------------------------------------------------------- #
# Shapes that must be caught
# --------------------------------------------------------------------------- #


def test_fstring_with_message_body_is_flagged() -> None:
    assert violations('logger.info(f"stored {body}")') == ["body"]


def test_fstring_with_secret_is_flagged() -> None:
    assert violations('logger.debug(f"using {api_key}")') == ["api_key"]


def test_percent_style_formatting_is_flagged() -> None:
    assert violations('logger.info("text: %s", transcript)') == ["transcript"]


def test_string_concatenation_is_flagged() -> None:
    assert violations('logger.warning("subject: " + email_subject)') == ["email_subject"]


def test_str_format_is_flagged() -> None:
    assert violations('logger.error("got {}".format(message_body))') == ["message_body"]


def test_attribute_access_is_flagged() -> None:
    assert violations('logger.info(f"{record.transcript}")') == ["transcript"]


def test_subscript_key_is_flagged() -> None:
    assert violations('logger.info(f"{payload[\'body\']}")') == ["body"]


def test_exception_and_critical_are_covered() -> None:
    assert violations('logger.exception(f"{body}")') == ["body"]
    assert violations('logger.critical(f"{body}")') == ["body"]


@pytest.mark.parametrize("receiver", ["logger", "log", "_logger", "self.log", "logging"])
def test_common_logger_receivers_are_recognised(receiver: str) -> None:
    assert violations(f'{receiver}.info(f"{{body}}")') == ["body"]


def test_multiple_distinct_leaks_are_all_reported() -> None:
    assert violations('logger.info(f"{body} {api_key}")') == ["body", "api_key"]


# --------------------------------------------------------------------------- #
# Shapes that must NOT be caught
# --------------------------------------------------------------------------- #


def test_structured_extra_is_the_supported_path() -> None:
    """This is what the runtime filter redacts — it must not be flagged."""
    assert violations('logger.info("stored", extra={"body": body})') == []


def test_log_context_helper_is_not_flagged() -> None:
    assert violations('logger.info("stored", extra=log_context(body=body))') == []


def test_content_summary_wrapper_is_allowed() -> None:
    """Wrapping in a redaction helper is the documented escape hatch."""
    assert violations('logger.info(f"{content_summary(body)}")') == []


def test_length_of_content_is_allowed() -> None:
    assert violations('logger.info(f"chars={len(transcript)}")') == []


def test_non_sensitive_names_pass() -> None:
    assert violations('logger.info(f"item {item_id} from {source}")') == []


def test_message_without_interpolation_passes() -> None:
    assert violations('logger.info("pipeline complete")') == []


def test_non_logger_calls_are_out_of_scope() -> None:
    """This rule is about logging; ``print`` is banned separately by ruff T20."""
    assert violations('print(f"{body}")') == []
    assert violations('some_function(f"{body}")') == []


# --------------------------------------------------------------------------- #
# Opt-out
# --------------------------------------------------------------------------- #


def test_noqa_with_rule_code_suppresses() -> None:
    assert violations(f'logger.debug(f"{{body}}")  # noqa: {RULE_CODE}') == []


def test_bare_noqa_suppresses() -> None:
    assert violations('logger.debug(f"{body}")  # noqa') == []


def test_unrelated_noqa_code_does_not_suppress() -> None:
    assert violations('logger.debug(f"{body}")  # noqa: E501') == ["body"]


def test_noqa_on_the_call_line_covers_a_wrapped_argument() -> None:
    source = 'logger.info(  # noqa: LOG001\n    f"{body}"\n)'
    assert violations(source) == []


# --------------------------------------------------------------------------- #
# Robustness and CLI
# --------------------------------------------------------------------------- #


def test_syntactically_invalid_source_does_not_crash() -> None:
    """Reporting syntax errors belongs to ruff and mypy, not this hook."""
    assert violations("def broken(:\n    pass") == []


def test_unreadable_and_empty_sources_are_safe() -> None:
    assert violations("") == []


def test_diagnostic_reports_path_line_and_column() -> None:
    found = check_source('x = 1\nlogger.info(f"{body}")\n', Path("example.py"))

    assert len(found) == 1
    diagnostic = found[0].as_diagnostic()
    assert diagnostic.startswith("example.py:2:")
    assert RULE_CODE in diagnostic
    assert "body" in diagnostic


def test_directory_scan_skips_caches(tmp_path: Path) -> None:
    (tmp_path / "clean.py").write_text('logger.info("ok")\n', encoding="utf-8")
    (tmp_path / "leaky.py").write_text('logger.info(f"{body}")\n', encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "stale.py").write_text('logger.info(f"{body}")\n', encoding="utf-8")

    found = check_paths([tmp_path])

    assert [v.path.name for v in found] == ["leaky.py"]


def test_main_returns_one_and_prints_when_violations_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "leaky.py").write_text('logger.info(f"{body}")\n', encoding="utf-8")

    assert main([str(tmp_path)]) == 1
    assert RULE_CODE in capsys.readouterr().out


def test_main_returns_zero_and_is_silent_when_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "clean.py").write_text('logger.info("ok")\n', encoding="utf-8")

    assert main([str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------- #
# The rule agrees with the runtime filter, and the codebase obeys it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name", ["body", "api_key", "transcript", "email_subject", "ocr_text", "password"]
)
def test_heuristic_shared_with_the_runtime_filter(name: str) -> None:
    """Markers are imported from catchment.redaction so the two cannot drift."""
    assert is_sensitive_name(name)


def test_the_codebase_is_clean() -> None:
    assert check_paths([REPO_PACKAGE]) == []
