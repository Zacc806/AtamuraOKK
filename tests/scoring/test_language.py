"""Tests for Kazakh-signal detection (drives Yandex SpeechKit escalation)."""

from __future__ import annotations

from AtamuraOKK.scoring.language import has_kazakh_signal


def test_kazakh_letters_signal() -> None:
    """Kazakh-specific Cyrillic letters are detected."""
    assert has_kazakh_signal("сәлеметсіз бе, әңгімелесейік") is True


def test_kazakh_function_word_signal() -> None:
    """A Kazakh function word amid Russian is detected."""
    assert has_kazakh_signal("привет менеджер керек ма") is True


def test_pure_russian_has_no_kazakh_signal() -> None:
    """Plain Russian text carries no Kazakh signal."""
    assert has_kazakh_signal("добрый день, расскажите про квартиру") is False


def test_empty_text_has_no_signal() -> None:
    """Empty text carries no Kazakh signal."""
    assert has_kazakh_signal("") is False
