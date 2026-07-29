#!/usr/bin/env python3
"""LOG001 — personal content or secrets interpolated into a log message string.

``RedactionFilter`` (``catchment/logging_config.py``) only ever sees the
structured ``extra=`` mapping on a ``LogRecord``. Anything the caller formats
into the message itself is already flat text by the time the filter runs::

    logger.info("stored", extra={"body": body})   # redacted on emit
    logger.info(f"stored body: {body}")           # leaks — this rule

The sensitive-name heuristic is imported from ``catchment.redaction`` rather
than restated here, so the static check and the runtime filter cannot drift.

Usage::

    python tools/check_log_fstrings.py [paths...]   # defaults to ``catchment``

Exits 1 with ``path:line:col: message`` diagnostics on stdout, 0 when clean.
Add ``# noqa: LOG001`` to the offending line (or the ``logger.*`` call line) to
opt out — DEBUG-level content logging is legitimate in local development.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Final, NamedTuple

from catchment.redaction import CONTENT_FIELD_MARKERS, SECRET_FIELD_MARKERS

RULE_CODE: Final[str] = "LOG001"

#: Single source of truth, shared with ``RedactionFilter``.
_MARKERS: Final[tuple[str, ...]] = SECRET_FIELD_MARKERS + CONTENT_FIELD_MARKERS

#: Receiver names treated as loggers, after stripping leading underscores.
_LOGGER_NAMES: Final[frozenset[str]] = frozenset({"logger", "log", "logging", "getlogger"})

_LOG_METHODS: Final[frozenset[str]] = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)

#: Helpers that already return a log-safe stand-in, so their arguments are fine.
_SAFE_CALLS: Final[frozenset[str]] = frozenset(
    {"content_summary", "content_digest", "redact_value", "redact_mapping", "len"}
)

_NOQA_RE: Final[re.Pattern[str]] = re.compile(
    r"#\s*noqa(?::\s*(?P<codes>[A-Za-z][A-Za-z0-9]*(?:[,\s]+[A-Za-z][A-Za-z0-9]*)*))?"
)

_SKIP_DIRS: Final[frozenset[str]] = frozenset(
    {".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
)

_DEFAULT_PATHS: Final[tuple[str, ...]] = ("catchment",)


class Violation(NamedTuple):
    """One sensitive reference reachable from a log call's message arguments."""

    path: Path
    line: int
    col: int
    name: str

    def as_diagnostic(self) -> str:
        """Render as ``path:line:col: message``, the standard linter shape."""
        return (
            f"{self.path}:{self.line}:{self.col}: {RULE_CODE} "
            f"{self.name!r} is interpolated into a log message and will bypass "
            f"RedactionFilter; pass it via extra={{...}} or wrap it in content_summary()"
        )


def is_sensitive_name(name: str) -> bool:
    """Return True if ``name`` looks like content or a credential."""
    lowered = name.lower()
    return any(marker in lowered for marker in _MARKERS)


def _referenced_name(node: ast.expr) -> str | None:
    """Return the field-like name a single expression node refers to, if any."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        key = node.slice.value
        return key if isinstance(key, str) else None
    return None


def _called_name(node: ast.expr) -> str | None:
    """Return the callee name when ``node`` is a call, else None."""
    return _referenced_name(node.func) if isinstance(node, ast.Call) else None


def _iter_sensitive(node: ast.expr) -> Iterator[tuple[str, ast.expr]]:
    """Yield every sensitive reference reachable from ``node``.

    Covers f-strings, ``%`` formatting, ``.format()`` and concatenation alike,
    because all four end up as ordinary expressions under the message argument.
    Descent stops at known-safe helper calls.
    """
    if _called_name(node) in _SAFE_CALLS:
        return
    name = _referenced_name(node)
    if name is not None and is_sensitive_name(name):
        yield name, node
        return
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.keyword):
            yield from _iter_sensitive(child.value)
        elif isinstance(child, ast.expr):
            yield from _iter_sensitive(child)


def _logger_receiver(func: ast.expr) -> str | None:
    """Return the normalised logger name when ``func`` is a ``logger.<level>``."""
    if not isinstance(func, ast.Attribute) or func.attr not in _LOG_METHODS:
        return None
    value = func.value
    if isinstance(value, ast.Call):
        value = value.func
    receiver = _referenced_name(value)
    if receiver is None:
        return None
    normalised = receiver.lstrip("_").lower()
    return normalised if normalised in _LOGGER_NAMES else None


def _has_noqa(line: str) -> bool:
    """Return True if ``line`` carries a bare ``# noqa`` or one naming LOG001."""
    for match in _NOQA_RE.finditer(line):
        codes = match.group("codes")
        if codes is None:
            return True
        if RULE_CODE in {code.upper() for code in re.split(r"[,\s]+", codes) if code}:
            return True
    return False


class _LogCallVisitor(ast.NodeVisitor):
    """Collect LOG001 violations from every logger call in a module."""

    def __init__(self, path: Path, lines: Sequence[str]) -> None:
        super().__init__()
        self._path = path
        self._lines = lines
        self.violations: list[Violation] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast dispatch name
        if _logger_receiver(node.func) is not None:
            self._check_message_args(node)
        self.generic_visit(node)

    def _check_message_args(self, node: ast.Call) -> None:
        """Scan positional args only: they are the message and its format args.

        Keyword arguments (``extra=``, ``exc_info=``) are the redacted path and
        are deliberately left alone.
        """
        seen: set[tuple[int, int, str]] = set()
        for argument in node.args:
            for name, found in _iter_sensitive(argument):
                key = (found.lineno, found.col_offset, name)
                if key in seen or self._suppressed(found.lineno, node.lineno):
                    continue
                seen.add(key)
                self.violations.append(
                    Violation(self._path, found.lineno, found.col_offset + 1, name)
                )

    def _suppressed(self, *linenos: int) -> bool:
        return any(_has_noqa(self._line(lineno)) for lineno in linenos)

    def _line(self, lineno: int) -> str:
        return self._lines[lineno - 1] if 0 < lineno <= len(self._lines) else ""


def check_source(source: str, path: Path) -> list[Violation]:
    """Return the violations in ``source``. Unparseable input yields none."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        # Reporting syntax errors is not this rule's job; ruff and mypy do it
        # with far better context. Never crash the hook over one bad file.
        return []
    visitor = _LogCallVisitor(path, source.splitlines())
    visitor.visit(tree)
    return visitor.violations


def check_file(path: Path) -> list[Violation]:
    """Return the violations in one file. Unreadable files yield none."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return check_source(source, path)


def iter_python_files(paths: Iterable[Path]) -> Iterator[Path]:
    """Yield every ``.py`` file under ``paths``, skipping caches and venvs."""
    for path in paths:
        if path.is_dir():
            for candidate in sorted(path.rglob("*.py")):
                if not _SKIP_DIRS.intersection(candidate.parts):
                    yield candidate
        elif path.suffix == ".py":
            yield path


def check_paths(paths: Iterable[Path]) -> list[Violation]:
    """Return every violation found under ``paths``."""
    return [violation for file in iter_python_files(paths) for violation in check_file(file)]


def main(argv: Sequence[str] | None = None) -> int:
    """Print diagnostics and return the process exit code."""
    arguments = tuple(argv) if argv is not None else tuple(sys.argv[1:])
    paths = [Path(argument) for argument in arguments or _DEFAULT_PATHS]
    violations = check_paths(paths)
    for violation in violations:
        sys.stdout.write(f"{violation.as_diagnostic()}\n")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
