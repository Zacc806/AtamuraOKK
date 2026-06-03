"""Tests for the language-routed scorer (dispatch + fallback)."""

from __future__ import annotations

from AtamuraOKK.scoring.base import CallForScoring, ScoreResult
from AtamuraOKK.scoring.errors import ProviderUnavailableError
from AtamuraOKK.scoring.router import LanguageRoutedScorer


def _result(provider: str) -> ScoreResult:
    """A minimal passing ScoreResult from the given provider."""
    return ScoreResult(
        rubric_version="tm_call_v2",
        total_score=80,
        max_total=100,
        score_pct=80.0,
        passed=True,
        criteria=[],
        client_agreed_meeting=True,
        manager_tone="вежливый",
        red_flags=[],
        summary="",
        language="ru",
        provider=provider,
        model="m",
    )


class _StubScorer:
    """A scorer stub that returns a preset result or raises."""

    def __init__(self, provider: str, *, raises: bool = False) -> None:
        self.provider = provider
        self._raises = raises
        self.called = False

    async def score(self, call: CallForScoring) -> ScoreResult:
        """Record the call and return a preset result (or raise)."""
        self.called = True
        if self._raises:
            raise ProviderUnavailableError(self.provider)
        return _result(self.provider)


def _ru_call() -> CallForScoring:
    """A confidently-Russian call."""
    return CallForScoring(
        text="добрый день расскажите про жк",
        duration_sec=120,
        language="ru",
        language_probability=0.95,
    )


def _kk_call() -> CallForScoring:
    """A Kazakh call."""
    return CallForScoring(text="сәлеметсіз бе", duration_sec=120, language="kk")


async def test_russian_routes_to_groq() -> None:
    """A Russian call is scored by the ru (Groq) scorer only."""
    ru, kk = _StubScorer("groq"), _StubScorer("yandex")
    result = await LanguageRoutedScorer(ru=ru, kk=kk).score(_ru_call())
    assert ru.called
    assert not kk.called
    assert result.language == "ru"
    assert result.provider == "groq"


async def test_kazakh_routes_to_yandex() -> None:
    """A Kazakh call is scored by the kk (Yandex) scorer only."""
    ru, kk = _StubScorer("groq"), _StubScorer("yandex")
    result = await LanguageRoutedScorer(ru=ru, kk=kk).score(_kk_call())
    assert kk.called
    assert not ru.called
    assert result.language == "kk"


async def test_fallback_when_primary_unavailable() -> None:
    """If the primary provider is down, the other scores and flags human review."""
    ru = _StubScorer("groq", raises=True)
    kk = _StubScorer("yandex")
    result = await LanguageRoutedScorer(ru=ru, kk=kk).score(_ru_call())
    assert ru.called
    assert kk.called
    assert result.needs_human_review is True
    assert result.meta["fallback_from"] == "groq"
    assert result.language == "ru"
