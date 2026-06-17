"""Brand/name correction for transcripts (post-STT).

Yandex SpeechKit's "general" model takes no custom vocabulary, so it mis-hears the
agency's own name: "Атамура" comes back as "атомра", "томра", "тамара", "самурай"
and similar. We cannot fix that acoustically, so we repair the known mis-hearings
in the recognized text after the fact, before the transcript is stored and scored.

ЖК (residential-complex) names transcribe cleanly today (Атмосфера, Керуен), so
they are not listed here; add them to ``_TERMS`` if a future sample shows drift.

Each entry maps a canonical term to the mis-heard forms to replace with it.
Matching is case-insensitive and whole-word. Note that some forms ("тамара",
"самурай") are also real words — they are included because in these telemarketing
calls the agency name dominates, but watch for false positives when validating
against the reference sample, and prune the entry if it costs more than it fixes.
"""

from __future__ import annotations

import re

# Canonical term -> mis-heard whole-word forms (case-insensitive).
_TERMS: dict[str, list[str]] = {
    "Атамура": ["атомра", "атамра", "томра", "тамра", "тамара", "самурай"],
}

# \b is Unicode-aware for str patterns, so it word-bounds Cyrillic correctly.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"\b(?:{'|'.join(forms)})\b", re.IGNORECASE), canonical)
    for canonical, forms in _TERMS.items()
]


def correct_terms(text: str) -> str:
    """Replace known brand/name mis-hearings with their canonical spelling."""
    for pattern, canonical in _PATTERNS:
        text = pattern.sub(canonical, text)
    return text
