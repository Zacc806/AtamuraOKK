"""Tests for reconstructing a ScoreResult from a persisted scores row."""

from __future__ import annotations

from AtamuraOKK.calibration.db_source import score_to_result
from AtamuraOKK.db.models.score import Score


def _score_row() -> Score:
    """An in-memory scores row (no session) mimicking a persisted meeting score."""
    return Score(
        call_id=1,
        rubric_version="okk_meeting_v1",
        total_score=84.0,
        score_pct=84.0,
        max_total=50,
        passed=True,
        criteria=[
            {"id": 1, "block": "Контакт", "name": "Приветствие", "score": 1,
             "max_score": 1, "auto": False},
            {"id": 4, "block": "Потребности", "name": "Критерии", "score": 3,
             "max_score": 5, "auto": False},
        ],
        summary="ок",
        flags=["груб"],
        call_type="первичная",
        client_agreed_meeting=True,
        manager_tone="вежливый",
        language="ru",
        provider="anthropic",
        needs_human_review=False,
        script_adherence=None,
        script_deviations=None,
        model="claude-sonnet-4-6",
        meta={"n_chunks": 3},
    )


def test_score_to_result_rebuilds_calibration_fields() -> None:
    """The fields the harness reads (score_pct/passed/criteria) are restored."""
    result = score_to_result(_score_row())

    assert result.score_pct == 84.0
    assert result.passed is True
    assert {c.id: c.score for c in result.criteria} == {1: 1, 4: 3}
    assert result.total_score == 4  # sum of criterion scores
    assert result.rubric_version == "okk_meeting_v1"


def test_score_to_result_tolerates_null_columns() -> None:
    """None JSONB columns degrade to safe empty defaults, not crashes."""
    row = Score(
        call_id=2,
        rubric_version="okk_meeting_v1",
        score_pct=None,
        passed=None,
        max_total=None,
        criteria=None,
        flags=None,
        script_deviations=None,
        meta=None,
    )

    result = score_to_result(row)

    assert result.criteria == []
    assert result.score_pct == 0.0
    assert result.passed is False
    assert result.red_flags == []
    assert result.script_deviations == []
    assert result.meta == {}
