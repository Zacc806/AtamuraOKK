"""Unit tests for the score assembly logic (no DB / no LLM)."""

from __future__ import annotations

from AtamuraOKK.scoring.base import CallScore, CriterionScore
from AtamuraOKK.scoring.rubric import Rubric, load_rubric
from AtamuraOKK.scoring.worker import _assemble

_OBJECTIONS_MAX = 21  # sum of objection-block criteria maxima in tm-call-v1


def _call_score(rubric: Rubric, *, objections_present: bool) -> CallScore:
    """A CallScore that awards full marks to every transcript-scored criterion."""
    return CallScore(
        call_type="квалификация",
        is_qualification_call=True,
        manager_identified=True,
        criteria=[
            CriterionScore(id=c.id, score=c.max, justification="ok", evidence="")
            for c in rubric.scored_criteria
        ],
        objections_present=objections_present,
        sentiment_customer="нейтральный",
        sentiment_agent="нейтральный",
        summary="тест",
        red_flags=[],
        target_status="неясно",
        strengths="-",
        growth_zone="-",
        training_recommendation="-",
    )


def test_objections_excluded_when_absent() -> None:
    """No objections occurred -> block drops out of numerator and denominator."""
    rubric = load_rubric()
    payload = _assemble(_call_score(rubric, objections_present=False), rubric)

    assert "objections" not in payload["blocks"]
    assert all(c["block_id"] != "objections" for c in payload["per_criterion"])
    assert payload["max_points"] == rubric.max_conversational - _OBJECTIONS_MAX
    assert payload["raw_points"] == payload["max_points"]
    assert payload["percent"] == 100.0


def test_objections_scored_when_present() -> None:
    """Objections occurred -> block is scored against the full conversational max."""
    rubric = load_rubric()
    payload = _assemble(_call_score(rubric, objections_present=True), rubric)

    assert "objections" in payload["blocks"]
    assert payload["max_points"] == rubric.max_conversational
    assert payload["percent"] == 100.0
