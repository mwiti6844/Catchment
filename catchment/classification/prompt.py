"""Prompt construction and response parsing for the tag classifier.

Both halves are pure functions so every decision path is testable against a
fixture without an LLM in the loop (CLAUDE.md). The model is asked for JSON and
its output is validated here — a malformed or hallucinated response must not
reach the tag graph.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Final

from catchment.classification.slug import slugify
from catchment.classification.types import TagSuggestion
from catchment.llm.types import Message

#: Text beyond this is truncated before it reaches the prompt. Classification
#: reads the gist; sending an entire transcript wastes tokens and can exceed
#: the model's window.
MAX_TEXT_CHARS: Final[int] = 6000

MAX_SUGGESTIONS: Final[int] = 8

SYSTEM_PROMPT: Final[str] = """\
You assign topic tags to saved content for a personal knowledge base.

You will be given an item's text and a list of tags already in use. Your job:

1. Prefer an existing tag whenever one genuinely fits. Reusing tags is what \
keeps the taxonomy navigable.
2. Coin a new tag only when the item is about a concept no existing tag covers. \
A new tag must be a durable topic, not a restatement of this one item.
3. Assign between 1 and 4 tags. Fewer, accurate tags beat many vague ones.
4. Never invent a tag that is a near-synonym of an existing one \
(e.g. "ML ops" when "MLOps" exists) - reuse the existing tag instead.

Return JSON only, matching this schema exactly:

{"tags": [{"label": "...", "confidence": 0.0-1.0, "is_new": true|false, \
"description": "..."}]}

- "label" is a short human-readable topic, in Title Case.
- "confidence" is how sure you are the tag applies to this item.
- "is_new" is true only if the label is not in the provided list.
- "description" is required for new tags and explains the concept in one \
sentence; omit it for existing tags.
"""


def truncate(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    """Trim text to a budget, marking that a cut happened."""
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "\n[truncated]"


def build_messages(text: str, *, known_tags: Sequence[str]) -> list[Message]:
    """Build the classification prompt.

    ``known_tags`` are the candidates retrieved from similar items. Passing them
    is what stops the model coining a duplicate of a tag it cannot see.
    """
    if known_tags:
        catalogue = "\n".join(f"- {tag}" for tag in known_tags)
    else:
        catalogue = "(none yet - this is an early item, so new tags are expected)"

    user = (
        f"Tags already in use:\n{catalogue}\n\n"
        f"Item text:\n---\n{truncate(text)}\n---\n\n"
        "Return the JSON object now."
    )
    return [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(role="user", content=user),
    ]


class ClassificationParseError(ValueError):
    """Raised when the model's response cannot be read as tag suggestions."""


def parse_response(raw: str, *, known_tags: Sequence[str] = ()) -> list[TagSuggestion]:
    """Parse and validate the model's JSON into suggestions.

    Everything that fails validation is dropped rather than raising, because one
    malformed entry should not discard the whole classification. A response with
    no usable entries at all does raise — that is a prompt or model problem
    worth surfacing.
    """
    payload = _load_json(raw)
    entries = payload.get("tags")
    if not isinstance(entries, list):
        raise ClassificationParseError("response has no 'tags' array")

    known_slugs = {slugify(tag) for tag in known_tags if _sluggable(tag)}
    suggestions: list[TagSuggestion] = []
    seen: set[str] = set()

    for entry in entries[:MAX_SUGGESTIONS]:
        suggestion = _to_suggestion(entry, known_slugs=known_slugs)
        if suggestion is None or suggestion.slug in seen:
            continue
        seen.add(suggestion.slug)
        suggestions.append(suggestion)

    if not suggestions:
        raise ClassificationParseError("response contained no usable tags")
    return suggestions


def _load_json(raw: str) -> dict[str, Any]:
    """Read JSON, tolerating a model that wrapped it in a markdown fence."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ClassificationParseError("response was not valid JSON") from error

    if not isinstance(payload, dict):
        raise ClassificationParseError("response was not a JSON object")
    return payload


def _sluggable(label: str) -> bool:
    try:
        slugify(label)
    except ValueError:
        return False
    return True


def _to_suggestion(entry: Any, *, known_slugs: set[str]) -> TagSuggestion | None:
    """Convert one entry, or None if it is unusable."""
    if not isinstance(entry, dict):
        return None

    label = entry.get("label")
    if not isinstance(label, str) or not _sluggable(label):
        return None

    confidence = entry.get("confidence")
    if not isinstance(confidence, int | float) or isinstance(confidence, bool):
        return None
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        return None

    description = entry.get("description")
    if not isinstance(description, str) or not description.strip():
        description = None

    # The model's own is_new is advisory; the candidate list is authoritative.
    # A model claiming a tag is new when we just showed it that tag would
    # otherwise coin a duplicate.
    is_new = slugify(label) not in known_slugs

    return TagSuggestion(
        label=label.strip(),
        confidence=confidence,
        is_new=is_new,
        description=description,
    )
