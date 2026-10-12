"""Conservative cleanup for final speech-to-text turns.

The provider remains the source of truth.  This module only fixes presentation
issues that are safe to apply without inventing words that were not spoken.
"""

from __future__ import annotations

import re
import unicodedata

_DISFLUENCY_WORDS = {
    "a",
    "and",
    "i",
    "là",
    "mà",
    "mình",
    "so",
    "the",
    "thì",
    "tôi",
    "uh",
    "um",
    "và",
    "well",
    "ờ",
    "ừ",
    "ừm",
}
_TRIPLE_WORD = re.compile(r"\b([^\W_]+)(?:\s+\1\b){2,}", re.IGNORECASE | re.UNICODE)
_DOUBLE_WORD = re.compile(
    r"\b(?P<word>[^\W_]+)(?P<space>\s+)(?P=word)\b", re.IGNORECASE | re.UNICODE
)


def normalize_transcript_text(text: str, *, final: bool = True) -> str:
    """Return readable transcript text while preserving the original meaning."""
    normalized = unicodedata.normalize("NFC", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = re.sub(r"\s+([,.;:!?])", r"\1", normalized)
    normalized = re.sub(r",(?=[^\s\d])", ", ", normalized)
    normalized = re.sub(r"([;:!?])(?=\S)", r"\1 ", normalized)
    normalized = _TRIPLE_WORD.sub(r"\1", normalized)

    # Collapse the most common short repeated hesitation words. Longer words
    # are left alone because repetition can be intentional emphasis.
    for _ in range(3):
        updated = _DOUBLE_WORD.sub(_collapse_short_disfluency, normalized)
        if updated == normalized:
            break
        normalized = updated

    normalized = _capitalize_sentence_starts(normalized)
    if final and normalized and normalized[-1].isalnum():
        normalized += "."
    return normalized


def _collapse_short_disfluency(match: re.Match[str]) -> str:
    word = match.group("word")
    if word.isalpha() and (len(word) <= 2 or word.casefold() in _DISFLUENCY_WORDS):
        return word
    return match.group(0)


def _capitalize_sentence_starts(text: str) -> str:
    characters = list(text)
    sentence_start = True
    pending_boundary = False
    for index, character in enumerate(characters):
        if character in ".!?:":
            pending_boundary = True
            continue
        if pending_boundary:
            if character.isspace():
                sentence_start = True
                continue
            if character in "\"'”’)]}":
                continue
            pending_boundary = False
        if sentence_start and character.isalpha():
            characters[index] = character.upper()
            sentence_start = False
        elif not character.isspace() and character not in "\"'“‘([{":
            sentence_start = False
    return "".join(characters)
