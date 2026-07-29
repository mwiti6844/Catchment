"""The degenerate extractor: the source already handed us text.

A WhatsApp text message or an image caption needs no OCR or transcription —
the text arrived with the webhook. This is deliberately *not* an
:class:`~catchment.extraction.Extractor`: that protocol takes a ``raw_ref``
pointing at a blob, and pretending inline text is a blob reference would
misuse the field. Real media extractors land in a later slice.
"""

from __future__ import annotations

from typing import Final

from catchment.extraction import ExtractionResult

PASSTHROUGH_EXTRACTOR: Final[str] = "passthrough"


def passthrough(text: str | None) -> ExtractionResult | None:
    """Wrap source-supplied text as an extraction result.

    Returns ``None`` for absent or whitespace-only text, so callers can treat
    "nothing to extract" as an ordinary outcome rather than an error — a
    captionless image is normal, not a failure.
    """
    if text is None:
        return None

    cleaned = text.strip()
    if not cleaned:
        return None

    return ExtractionResult(
        extractor=PASSTHROUGH_EXTRACTOR,
        text=cleaned,
        confidence=1.0,
        meta={"reason": "source supplied text directly"},
    )
