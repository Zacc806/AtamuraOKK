"""Split a long meeting transcript into LLM-sized chunks (Этап 3, ОП-встречи).

ОП meetings run 40-90 min, so a single transcript easily exceeds the model's
useful context. We split on line (speaker-utterance) boundaries — keeping a
small line overlap so a thought spanning a boundary survives in both chunks.
A single line longer than the cap (e.g. an unsegmented STT transcript with no
newlines at all) is itself split on sentence, then whitespace, boundaries —
**no input can make a chunk exceed the cap**, because oversized chunks get
silently truncated downstream. Pure function, fully unit-testable without any
network.
"""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def _split_long_line(line: str, max_chars: int) -> list[str]:
    """Split one oversized line into ``<= max_chars`` pieces.

    Prefers sentence boundaries, then whitespace; a single unbreakable token
    longer than the cap is hard-cut (cannot happen with real speech).
    """
    if len(line) <= max_chars:
        return [line]

    pieces: list[str] = []
    current = ""
    for part in _SENTENCE_END.split(line):
        words = part.split() if len(part) > max_chars else [part]
        for word in words:
            if len(word) > max_chars:  # unbreakable token — hard cut
                if current:
                    pieces.append(current)
                    current = ""
                pieces.extend(
                    word[i : i + max_chars] for i in range(0, len(word), max_chars)
                )
                continue
            candidate = f"{current} {word}" if current else word
            if len(candidate) > max_chars:
                pieces.append(current)
                current = word
            else:
                current = candidate
    if current:
        pieces.append(current)
    return pieces


def chunk_transcript(
    text: str,
    *,
    max_chars: int,
    overlap_lines: int = 1,
) -> list[str]:
    r"""Split a speaker-tagged transcript into ``<= max_chars`` chunks.

    :param text: the full transcript ("[agent] ...\n[customer] ...").
    :param max_chars: size cap per chunk. Lines longer than the cap are split
        on sentence/whitespace boundaries first, so the cap always holds.
    :param overlap_lines: trailing lines of each chunk repeated at the start of
        the next, for cross-boundary context.
    :returns: ordered chunks; ``[]`` for blank input, ``[text]`` if it fits.
    """
    if max_chars <= 0:
        msg = "max_chars must be positive"
        raise ValueError(msg)
    if not text.strip():
        return []
    if len(text) <= max_chars:
        return [text]

    lines = [
        piece
        for raw_line in text.splitlines()
        for piece in _split_long_line(raw_line, max_chars)
    ]

    chunks: list[str] = []
    current: list[str] = []
    cur_len = 0

    for line in lines:
        piece = len(line) + (1 if current else 0)
        if current and cur_len + piece > max_chars:
            chunks.append("\n".join(current))
            current = current[-overlap_lines:] if overlap_lines > 0 else []
            cur_len = len("\n".join(current))
            piece = len(line) + (1 if current else 0)
            # Drop the overlap seed if it would itself push past the cap, so the
            # documented `<= max_chars` guarantee holds.
            if current and cur_len + piece > max_chars:
                current = []
                cur_len = 0
                piece = len(line)
        current.append(line)
        cur_len += piece

    if current:
        chunks.append("\n".join(current))
    return chunks
