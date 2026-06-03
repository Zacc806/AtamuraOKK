"""Tests for the Groq scorer transport mapping (mocked client)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from AtamuraOKK.scoring.base import CallForScoring
from AtamuraOKK.scoring.errors import ScoringError
from AtamuraOKK.scoring.groq import GroqScorer
from AtamuraOKK.scoring.rubric import load_rubric

RUBRIC = load_rubric("tm_call_v2")

_CONTENT = (
    '{"scores": {"1": 1}, "client_agreed_meeting": false, '
    '"manager_tone": "нейтральный", "red_flags_found": [], "summary": "x"}'
)


def _fake_client(content: str) -> SimpleNamespace:
    """Build a stand-in AsyncGroq client returning fixed completion content."""

    async def _create(**_: object) -> SimpleNamespace:
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    completions = SimpleNamespace(create=_create)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def _call() -> CallForScoring:
    """A minimal Russian call to score."""
    return CallForScoring(text="[agent] привет", duration_sec=120, language="ru")


async def test_maps_groq_response() -> None:
    """A Groq chat completion is parsed into a ScoreResult."""
    scorer = GroqScorer(
        RUBRIC,
        client=_fake_client(_CONTENT),  # type: ignore[arg-type]
        max_retries=1,
        retry_base_delay=0.0,
    )
    result = await scorer.score(_call())
    assert result.provider == "groq"
    assert len(result.criteria) == len(RUBRIC.criteria)


async def test_empty_content_raises() -> None:
    """Empty content from Groq raises a ScoringError."""
    scorer = GroqScorer(
        RUBRIC,
        client=_fake_client(""),  # type: ignore[arg-type]
        max_retries=1,
        retry_base_delay=0.0,
    )
    with pytest.raises(ScoringError):
        await scorer.score(_call())
