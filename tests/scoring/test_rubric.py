"""Tests for the rubric loader."""

from __future__ import annotations

import pytest

from AtamuraOKK.scoring.rubric import load_rubric

RUBRIC = "tm_call_v2"


def test_loads_and_validates() -> None:
    """Rubric loads, exposes metadata, and criteria maxima sum to the total."""
    rubric = load_rubric(RUBRIC)
    assert rubric.id == RUBRIC
    assert rubric.max_total_score == 100
    assert len(rubric.criteria) == 21
    assert sum(c.max_score for c in rubric.criteria) == 100


def test_ai_criteria_exclude_auto_check() -> None:
    """auto_check criteria (7, 19, 20) are excluded from the LLM-scored set."""
    rubric = load_rubric(RUBRIC)
    ai_ids = {c.id for c in rubric.ai_criteria}
    assert {7, 19, 20}.isdisjoint(ai_ids)
    assert len(rubric.ai_criteria) == 18


def test_auto_scores_duration_gate() -> None:
    """The duration<=300 rule awards full/zero; default_full is unconditional."""
    rubric = load_rubric(RUBRIC)
    short = rubric.auto_scores(duration_sec=200)
    assert short[7] == 4
    assert short[19] == 2
    assert short[20] == 5

    long = rubric.auto_scores(duration_sec=400)
    assert long[7] == 0
    assert long[19] == 2


def test_by_id_indexes_all() -> None:
    """by_id maps every criterion id; the closing criterion is the heaviest."""
    rubric = load_rubric(RUBRIC)
    assert set(rubric.by_id) == {c.id for c in rubric.criteria}
    assert rubric.by_id[10].max_score == 15


def test_tm_rubric_objection_block() -> None:
    """The TM rubric names its conditional objections block."""
    rubric = load_rubric(RUBRIC)
    assert rubric.objection_block == "Отработка возражений"


def test_okk_meeting_rubric_loads() -> None:
    """The OP-meeting rubric (from the xlsx) loads with 20 criteria summing to 50."""
    rubric = load_rubric("okk_meeting_v1")
    assert rubric.max_total_score == 50
    assert len(rubric.criteria) == 20
    assert sum(c.max_score for c in rubric.criteria) == 50
    # All criteria are LLM-scored (no auto_check on the meeting checklist).
    assert rubric.ai_criteria == rubric.criteria
    assert rubric.objection_block == "Возражения"
    assert rubric.blocks["Возражения"] == [12, 13, 14, 15]


def test_tm_call_v3_loads() -> None:
    """The updated call checklist (измененные поля) loads: 21 crit, max 100."""
    rubric = load_rubric("tm_call_v3")
    assert rubric.max_total_score == 100
    assert len(rubric.criteria) == 21
    assert sum(c.max_score for c in rubric.criteria) == 100
    # CRM-discipline criteria 19, 20 are auto (not checkable from call audio).
    auto = rubric.auto_scores(duration_sec=120)
    assert auto == {19: 2, 20: 5}
    assert {c.id for c in rubric.ai_criteria}.isdisjoint({19, 20})
    assert rubric.objection_block == "Отработка возражений"


def test_missing_rubric_raises() -> None:
    """Loading an unknown rubric version raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_rubric("does_not_exist")
