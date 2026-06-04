"""Tests for Whisper hallucination cleanup."""

from __future__ import annotations

from AtamuraOKK.transcription.cleanup import clean_transcript


def test_removes_blacklisted_phrase() -> None:
    """A line that is only a Whisper hallucination is dropped."""
    text = (
        "[agent] Здравствуйте, ATAMURA\n"
        "[customer] Спасибо за просмотр\n"
        "[agent] Расскажу о ЖК"
    )
    out = clean_transcript(text)
    assert "Спасибо за просмотр" not in out
    assert "Расскажу о ЖК" in out
    assert "[customer]" not in out


def test_collapses_duplicate_lines() -> None:
    """Consecutive duplicate farewells collapse to one."""
    text = "[agent] до свидания\n[agent] до свидания\n[agent] до свидания"
    out = clean_transcript(text)
    assert out.count("до свидания") == 1


def test_keeps_clean_transcript() -> None:
    """A clean transcript is returned unchanged."""
    text = "[agent] добрый день\n[customer] здравствуйте"
    assert clean_transcript(text) == text
