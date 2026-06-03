"""Tests for assembling a ScoreResult from LLM output + rubric."""

from __future__ import annotations

from AtamuraOKK.scoring.base import CallForScoring, ScoreResult
from AtamuraOKK.scoring.result import assemble_score
from AtamuraOKK.scoring.rubric import Rubric, load_rubric
from AtamuraOKK.scoring.schema import LLMScore

RUBRIC = load_rubric("tm_call_v2")
THRESHOLD = 75


def _full_scores(rubric: Rubric) -> dict[str, int]:
    """Build an LLM scores dict awarding every AI criterion its max."""
    return {str(c.id): c.max_score for c in rubric.ai_criteria}


def _call(duration_sec: int = 120) -> CallForScoring:
    """A minimal Russian call of the given duration."""
    return CallForScoring(
        text="[agent] привет",
        duration_sec=duration_sec,
        language="ru",
    )


def _assemble(llm: LLMScore, *, duration_sec: int = 120) -> ScoreResult:
    """Assemble a ScoreResult for the test rubric and a call of given duration."""
    return assemble_score(
        llm,
        rubric=RUBRIC,
        call=_call(duration_sec),
        language="ru",
        provider="groq",
        model="test",
        pass_threshold=THRESHOLD,
    )


def test_full_marks_short_call_is_100() -> None:
    """All criteria at max on a short call yields 100 and a pass."""
    result = _assemble(LLMScore(scores=_full_scores(RUBRIC)))
    assert result.total_score == 100
    assert result.score_pct == 100.0
    assert result.passed is True
    autos = {c.id: c.auto for c in result.criteria}
    assert autos[7] is True
    assert autos[19] is True
    assert autos[1] is False


def test_long_call_loses_duration_criterion() -> None:
    """A call over 5 minutes zeroes the duration criterion (max 4)."""
    result = _assemble(LLMScore(scores=_full_scores(RUBRIC)), duration_sec=400)
    assert result.total_score == 96


def test_missing_criteria_recorded_and_zeroed() -> None:
    """An omitted criterion is scored 0 and recorded in meta."""
    scores = _full_scores(RUBRIC)
    del scores["1"]
    result = _assemble(LLMScore(scores=scores))
    assert result.total_score == 99
    assert result.meta["missing_criteria"] == [1]
    assert result.needs_human_review is False


def test_three_missing_criteria_flags_human_review() -> None:
    """Three or more omitted criteria flag the call for human review."""
    scores = _full_scores(RUBRIC)
    for cid in ("1", "2", "3"):
        del scores[cid]
    result = _assemble(LLMScore(scores=scores))
    assert sorted(result.meta["missing_criteria"]) == [1, 2, 3]
    assert result.needs_human_review is True


def test_out_of_range_scores_clamped() -> None:
    """Scores outside [0, max] are clamped and counted in meta."""
    scores = _full_scores(RUBRIC)
    scores["10"] = 999
    scores["2"] = -5
    result = _assemble(LLMScore(scores=scores))
    by_id = {c.id: c.score for c in result.criteria}
    assert by_id[10] == 15
    assert by_id[2] == 0
    assert result.meta["clamped_criteria"] == 2


def test_low_score_does_not_pass() -> None:
    """A near-empty score (only default_full autos) fails the threshold."""
    result = _assemble(LLMScore(scores={}), duration_sec=400)
    assert result.total_score == 7
    assert result.passed is False
