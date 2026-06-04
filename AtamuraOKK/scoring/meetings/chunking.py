"""Split a long meeting transcript into LLM-sized chunks (Этап 3, ОП-встречи).

ОП meetings run 40-90 min, so a single transcript easily exceeds the model's
useful context. We split on line (speaker-utterance) boundaries — never mid-word
— keeping a small line overlap so a thought spanning a boundary survives in both
chunks. Pure function, fully unit-testable without any network.
"""

from __future__ import annotations


def chunk_transcript(
    text: str,
    *,
    max_chars: int,
    overlap_lines: int = 1,
) -> list[str]:
    r"""Split a speaker-tagged transcript into ``<= max_chars`` chunks.

    :param text: the full transcript ("[agent] ...\n[customer] ...").
    :param max_chars: soft size cap per chunk (a single oversized line may exceed
        it — we never split inside a line).
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

    chunks: list[str] = []
    current: list[str] = []
    cur_len = 0

    for line in text.splitlines():
        piece = len(line) + (1 if current else 0)
        if current and cur_len + piece > max_chars:
            chunks.append("\n".join(current))
            current = current[-overlap_lines:] if overlap_lines > 0 else []
            cur_len = len("\n".join(current))
            piece = len(line) + (1 if current else 0)
            # Drop the overlap seed if it would itself push past the cap, so the
            # documented `<= max_chars` guarantee holds (bar one oversized line).
            if current and cur_len + piece > max_chars:
                current = []
                cur_len = 0
                piece = len(line)
        current.append(line)
        cur_len += piece

    if current:
        chunks.append("\n".join(current))
    return chunks
