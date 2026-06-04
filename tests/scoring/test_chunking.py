"""Tests for the meeting-transcript chunker."""

from __future__ import annotations

import pytest

from AtamuraOKK.scoring.chunking import chunk_transcript


def test_blank_returns_empty() -> None:
    """Whitespace-only input yields no chunks."""
    assert chunk_transcript("   \n  ", max_chars=100) == []


def test_short_text_is_one_chunk() -> None:
    """Text within the cap is returned unchanged as a single chunk."""
    text = "[agent] привет\n[client] здравствуйте"
    assert chunk_transcript(text, max_chars=1000) == [text]


def test_long_text_splits_on_line_boundaries() -> None:
    """A long transcript splits into capped chunks without breaking lines."""
    lines = [f"[agent] строка номер {i} с текстом" for i in range(50)]
    text = "\n".join(lines)

    chunks = chunk_transcript(text, max_chars=120, overlap_lines=0)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 120
    rejoined = "\n".join(chunks)
    for line in lines:
        assert line in rejoined


def test_overlap_repeats_boundary_line() -> None:
    """With overlap_lines=1 the last line of a chunk opens the next chunk."""
    lines = [f"L{i}-" + ("x" * 20) for i in range(20)]
    text = "\n".join(lines)

    chunks = chunk_transcript(text, max_chars=80, overlap_lines=1)

    assert len(chunks) >= 2
    assert chunks[1].splitlines()[0] == chunks[0].splitlines()[-1]


def test_overlap_never_exceeds_cap() -> None:
    """No chunk exceeds the cap even with overlap (overlap dropped if it would)."""
    lines = ["x" * 44 for _ in range(8)]  # each line < cap, but line+overlap > cap
    text = "\n".join(lines)

    chunks = chunk_transcript(text, max_chars=60, overlap_lines=1)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 60


def test_invalid_max_chars_raises() -> None:
    """A non-positive cap is rejected."""
    with pytest.raises(ValueError, match="max_chars"):
        chunk_transcript("x", max_chars=0)
