"""C6 regression: reject truncated / incomplete scores.

``_assemble`` raises when the model omitted a scored (non-objection) criterion
rather than scoring it 0, and ``AnthropicScorer.score`` rejects a ``max_tokens``
truncation rather than validating the truncated tool input.
"""

from __future__ import annotations

from typing import Any

import pytest

from AtamuraOKK.scoring.anthropic_scorer import AnthropicScorer
from AtamuraOKK.scoring.base import CallScore, CriterionScore
from AtamuraOKK.scoring.rubric import Rubric, load_rubric
from AtamuraOKK.scoring.worker import _assemble


def _full_call_score(rubric: Rubric, *, drop_first: bool = False) -> CallScore:
    criteria = [
        CriterionScore(
            id=c.id, score=c.max, justification="ok", evidence="", recommendation="-"
        )
        for c in rubric.scored_criteria
    ]
    if drop_first:
        criteria = criteria[1:]  # omit one scored criterion the model "forgot"
    return CallScore(
        call_type="квалификация",
        is_qualification_call=True,
        manager_identified=True,
        criteria=criteria,
        objections_present=True,
        sentiment_customer="нейтральный",
        sentiment_agent="нейтральный",
        summary="тест",
        red_flags=[],
        target_status="неясно",
        strengths="-",
        growth_zone="-",
        training_recommendation="-",
    )


def test_assemble_raises_on_missing_criterion() -> None:
    """A criterion the model didn't return fails the call instead of scoring 0."""
    rubric = load_rubric()
    with pytest.raises(ValueError, match="omitted criteria"):
        _assemble(_full_call_score(rubric, drop_first=True), rubric)


def test_assemble_ok_when_complete() -> None:
    """A complete set of criteria assembles normally."""
    rubric = load_rubric()
    payload = _assemble(_full_call_score(rubric), rubric)
    assert payload["percent"] == 100.0


class _FakeResponse:
    def __init__(self, stop_reason: str, content: list[Any]) -> None:
        self.stop_reason = stop_reason
        self.content = content


class _FakeMessages:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def create(self, **_: Any) -> _FakeResponse:
        return self._response


class _FakeAnthropic:
    def __init__(self, response: _FakeResponse) -> None:
        self.messages = _FakeMessages(response)


async def test_scorer_rejects_max_tokens_truncation() -> None:
    """A max_tokens stop_reason raises instead of validating truncated output."""
    rubric = load_rubric()
    scorer = AnthropicScorer(model="test")
    scorer._client = _FakeAnthropic(_FakeResponse("max_tokens", []))  # type: ignore[assignment]  # noqa: SLF001

    with pytest.raises(RuntimeError, match="max_tokens"):
        await scorer.score(transcript="привет", rubric=rubric, direction="outbound")
