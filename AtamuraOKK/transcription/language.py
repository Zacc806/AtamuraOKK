"""Lightweight RU/KK language detection from transcript text.

gpt-4o-transcribe returns no language field, so we infer it from the output.
Kazakh uses Cyrillic letters that Russian does not (ә ғ қ ң ө ұ ү һ і); their
presence above a small threshold is a strong, dependency-free signal. Used to
route Kazakh calls to a held state until a Kazakh STT provider is available.
"""

from __future__ import annotations

# Letters in the Kazakh Cyrillic alphabet that do not occur in Russian.
KAZAKH_ONLY_LETTERS = set("әғқңөұүһіӘҒҚҢӨҰҮҺІ")
# Treat as Kazakh once Kazakh-only letters make up at least this share of all
# Cyrillic letters (guards against an occasional stray glyph in Russian text).
_KK_RATIO_THRESHOLD = 0.01
_KK_MIN_COUNT = 3


def detect_language(text: str) -> str:
    """Return ``"kk"``, ``"ru"``, or ``"unknown"`` for a transcript."""
    cyrillic = 0
    kazakh = 0
    for ch in text:
        if "Ѐ" <= ch <= "ӿ":
            cyrillic += 1
            if ch in KAZAKH_ONLY_LETTERS:
                kazakh += 1
    if cyrillic == 0:
        return "unknown"
    if kazakh >= _KK_MIN_COUNT and kazakh / cyrillic >= _KK_RATIO_THRESHOLD:
        return "kk"
    return "ru"
