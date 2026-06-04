"""Kazakh-signal detection over transcript text.

Whisper/gpt-4o-transcribe label mixed "шала казахский" speech inconsistently
(often ``ru`` with low confidence), so the transcription router combines the
detected language with this cheap script/lexicon check to decide whether to
escalate a recording to Yandex SpeechKit. Pure function, trivially testable.
"""

from __future__ import annotations

import re

# Kazakh-specific Cyrillic letters absent from Russian.
_KK_LETTERS = frozenset("әғқңөұүһі")
# Common Kazakh function words (co-occur with Russian tokens in shala speech).
_KK_WORDS = frozenset(
    {"және", "бар", "жоқ", "ма", "ме", "ғой", "керек", "үшін", "болады", "емес"},
)
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def has_kazakh_signal(text: str) -> bool:
    """True if the text shows Kazakh-specific letters or function words."""
    low = text.lower()
    if any(ch in _KK_LETTERS for ch in low):
        return True
    return bool(set(_WORD_RE.findall(low)) & _KK_WORDS)
