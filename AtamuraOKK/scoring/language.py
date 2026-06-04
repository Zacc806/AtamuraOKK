"""Language routing: decide ru -> Groq vs kk/shala -> Yandex.

Whisper labels mixed "шала казахский" speech inconsistently (often ru with low
confidence), so we combine the detected language + probability with a cheap
script/lexicon check over the transcript. Pure function, trivially testable.
"""

from __future__ import annotations

import re
from typing import Literal

from AtamuraOKK.scoring.base import CallForScoring

Lang = Literal["ru", "kk", "shala"]

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


def route(call: CallForScoring, *, confidence_threshold: float) -> Lang:
    """Route a call to a scoring language bucket.

    :param call: the call (detected language + probability + text).
    :param confidence_threshold: min probability to trust a "ru" detection.
    :returns: ``"ru"`` (Groq), or ``"kk"``/``"shala"`` (Yandex).
    """
    lang = (call.language or "auto").lower()
    if lang.startswith("kk"):
        return "kk"
    if has_kazakh_signal(call.text):
        return "shala"
    if lang.startswith("ru"):
        return "ru" if call.language_probability >= confidence_threshold else "shala"
    return "ru"
