"""Tests for the language router (ru -> Groq, kk/shala -> Yandex)."""

from __future__ import annotations

from AtamuraOKK.scoring.base import CallForScoring
from AtamuraOKK.scoring.language import has_kazakh_signal, route

THRESHOLD = 0.75


def _call(text: str, language: str, prob: float) -> CallForScoring:
    """Build a call with the given text, detected language, and probability."""
    return CallForScoring(
        text=text,
        duration_sec=100,
        language=language,
        language_probability=prob,
    )


def test_confident_russian_routes_ru() -> None:
    """Confident Russian detection routes to the Russian (Groq) scorer."""
    call = _call("здравствуйте, как ваши дела сегодня", "ru", 0.95)
    assert route(call, confidence_threshold=THRESHOLD) == "ru"


def test_low_confidence_russian_routes_shala() -> None:
    """Low-confidence Russian routes to shala (Yandex), the safer Kazakh path."""
    call = _call("здравствуйте как дела", "ru", 0.4)
    assert route(call, confidence_threshold=THRESHOLD) == "shala"


def test_detected_kazakh_routes_kk() -> None:
    """A Kazakh detection routes to the Kazakh (Yandex) scorer."""
    call = _call("текст", "kk", 0.9)
    assert route(call, confidence_threshold=THRESHOLD) == "kk"


def test_kazakh_letters_override_ru_detection() -> None:
    """Kazakh-specific letters reroute a confident-Russian detection to shala."""
    call = _call("сәлеметсіз бе, әңгімелесейік", "ru", 0.95)
    assert route(call, confidence_threshold=THRESHOLD) == "shala"


def test_kazakh_function_word_signals_shala() -> None:
    """A Kazakh function word amid Russian is detected and routes to shala."""
    assert has_kazakh_signal("привет менеджер керек ма") is True
    call = _call("привет менеджер керек ма", "ru", 0.95)
    assert route(call, confidence_threshold=THRESHOLD) == "shala"


def test_pure_russian_has_no_kazakh_signal() -> None:
    """Plain Russian text carries no Kazakh signal."""
    assert has_kazakh_signal("добрый день, расскажите про квартиру") is False


def test_auto_unknown_defaults_to_ru() -> None:
    """An auto/unknown language with no Kazakh signal defaults to Russian."""
    call = _call("добрый день расскажите про жк", "auto", 1.0)
    assert route(call, confidence_threshold=THRESHOLD) == "ru"
