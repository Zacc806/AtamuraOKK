"""Tests for the Yandex scorer transport (httpx MockTransport)."""

from __future__ import annotations

import httpx
import pytest

from AtamuraOKK.scoring.base import CallForScoring
from AtamuraOKK.scoring.errors import ProviderUnavailableError, ScoringError
from AtamuraOKK.scoring.rubric import load_rubric
from AtamuraOKK.scoring.yandex import YandexScorer

RUBRIC = load_rubric("tm_call_v2")

_PAYLOAD = (
    '{"scores": {"1": 1}, "client_agreed_meeting": true, '
    '"manager_tone": "вежливый", "red_flags_found": [], "summary": "ok"}'
)


def _body(text: str) -> dict[str, object]:
    """A well-formed YandexGPT completion envelope wrapping ``text``."""
    return {"result": {"alternatives": [{"message": {"text": text}}]}}


def _scorer(handler: object) -> YandexScorer:
    """A YandexScorer backed by a MockTransport using ``handler``."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return YandexScorer(RUBRIC, client=client, max_retries=1, retry_base_delay=0.0)


def _call() -> CallForScoring:
    """A minimal Kazakh call to score."""
    return CallForScoring(text="[agent] сәлеметсіз бе", duration_sec=120, language="kk")


async def test_maps_yandex_response() -> None:
    """A 200 YandexGPT response is parsed into a ScoreResult."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_body(_PAYLOAD))

    result = await _scorer(handler).score(_call())
    assert result.provider == "yandex"
    assert len(result.criteria) == len(RUBRIC.criteria)


async def test_429_raises_provider_unavailable() -> None:
    """A 429 maps to ProviderUnavailableError."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate"})

    with pytest.raises(ProviderUnavailableError):
        await _scorer(handler).score(_call())


async def test_bad_shape_raises_scoring_error() -> None:
    """An unexpected 200 body shape raises ScoringError."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    with pytest.raises(ScoringError):
        await _scorer(handler).score(_call())
