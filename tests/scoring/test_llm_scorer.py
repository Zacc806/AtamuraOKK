"""Tests for the shared LLM-scorer retry/parse machinery."""

from __future__ import annotations

import pytest

from AtamuraOKK.scoring.meetings.base import CallForScoring
from AtamuraOKK.scoring.meetings.errors import (
    MalformedOutputError,
    ProviderUnavailableError,
)
from AtamuraOKK.scoring.meetings.llm import BaseLLMScorer
from AtamuraOKK.scoring.meetings.rubric import load_rubric

RUBRIC = load_rubric("tm_call_v2")


def _valid_json() -> str:
    """A complete, valid LLM response covering every AI criterion."""
    scores = ", ".join(f'"{c.id}": {c.max_score}' for c in RUBRIC.ai_criteria)
    return (
        f'{{"scores": {{{scores}}}, "client_agreed_meeting": true, '
        '"manager_tone": "вежливый", "red_flags_found": [], "summary": "ок"}'
    )


class _ScriptedScorer(BaseLLMScorer):
    """Scorer whose _raw_complete replays a scripted list of responses/errors."""

    provider = "fake"

    def __init__(self, script: list[str | Exception]) -> None:
        super().__init__(RUBRIC, model="fake", max_retries=3, retry_base_delay=0.0)
        self._script = list(script)
        self.calls = 0

    async def _raw_complete(self, prompt: str) -> str:
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _call() -> CallForScoring:
    """A minimal Russian call to score."""
    return CallForScoring(
        text="[agent] привет [customer] да",
        duration_sec=120,
        language="ru",
    )


async def test_happy_path() -> None:
    """A valid first response scores in a single attempt."""
    scorer = _ScriptedScorer([_valid_json()])
    result = await scorer.score(_call())
    assert result.total_score == 100
    assert result.meta["attempts"] == 1
    assert scorer.calls == 1


async def test_retries_then_succeeds_on_malformed() -> None:
    """A malformed answer is retried, then a valid one succeeds."""
    scorer = _ScriptedScorer(["garbage no json", _valid_json()])
    result = await scorer.score(_call())
    assert result.meta["attempts"] == 2
    assert scorer.calls == 2


async def test_malformed_exhausts_retries() -> None:
    """Persistent malformed output raises after max_retries."""
    scorer = _ScriptedScorer(["x", "y", "z"])
    with pytest.raises(MalformedOutputError):
        await scorer.score(_call())
    assert scorer.calls == 3


async def test_provider_unavailable_then_succeeds() -> None:
    """A transient provider error is retried, then a valid one succeeds."""
    scorer = _ScriptedScorer([ProviderUnavailableError("429"), _valid_json()])
    result = await scorer.score(_call())
    assert result.meta["attempts"] == 2


async def test_provider_unavailable_exhausts() -> None:
    """Persistent provider unavailability raises after max_retries."""
    scorer = _ScriptedScorer(
        [
            ProviderUnavailableError("a"),
            ProviderUnavailableError("b"),
            ProviderUnavailableError("c"),
        ],
    )
    with pytest.raises(ProviderUnavailableError):
        await scorer.score(_call())
    assert scorer.calls == 3
