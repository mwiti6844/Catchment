"""Text recovery: article parsing, OCR (PaddleOCR-VL), transcription (faster-whisper).

Model runtimes are optional dependencies (``pip install -e '.[extraction]'``)
so the core package stays installable without them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Text recovered from one item by one extractor."""

    extractor: str
    text: str
    language: str | None = None
    confidence: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Extractor(Protocol):
    """Turns an item's raw payload into text.

    Implementations must not log the text they produce — pass it back and let
    the repository layer store it.
    """

    name: str

    def supports(self, kind: str) -> bool:
        """Return True if this extractor handles the given item kind."""
        ...

    def extract(self, raw_ref: str) -> ExtractionResult:
        """Extract text from the blob at ``raw_ref``."""
        ...


__all__ = ["ExtractionResult", "Extractor"]
