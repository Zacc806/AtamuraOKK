"""Tests for the Anthropic scorer transport (mocked client)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from AtamuraOKK.scoring.meetings.anthropic import AnthropicScorer
from AtamuraOKK.scoring.meetings.base import CallForScoring
from AtamuraOKK.scoring.meetings.errors import ScoringError
from AtamuraOKK.scoring.meetings.rubric import load_rubric

RUBRIC = load_rubric("okk_meeting_v1")

_CONTENT = (
    '{"scores": {"1": 1}, "client_agreed_meeting": true, '
    '"manager_tone": "вежливый", "red_flags_found": [], "summary": "ok"}'
)


def _fake_client(text: str) -> SimpleNamespace:
    """A stand-in AsyncAnthropic client returning fixed message content."""

    async def _create(**_: object) -> SimpleNamespace:
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    return SimpleNamespace(messages=SimpleNamespace(create=_create))


def _call() -> CallForScoring:
    """A minimal Russian call to score."""
    return CallForScoring(text="[agent] привет", duration_sec=120, language="ru")


async def test_maps_anthropic_response() -> None:
    """A Claude message is parsed into a ScoreResult under the meeting rubric."""
    scorer = AnthropicScorer(
        RUBRIC,
        client=_fake_client(_CONTENT),  # type: ignore[arg-type]
        max_retries=1,
        retry_base_delay=0.0,
    )
    result = await scorer.score(_call())
    assert result.provider == "anthropic"
    assert result.rubric_version == "okk_meeting_v1"
    assert len(result.criteria) == len(RUBRIC.criteria)


async def test_empty_content_raises() -> None:
    """An empty Claude response raises a ScoringError."""
    scorer = AnthropicScorer(
        RUBRIC,
        client=_fake_client(""),  # type: ignore[arg-type]
        max_retries=1,
        retry_base_delay=0.0,
    )
    with pytest.raises(ScoringError):
        await scorer.score(_call())
