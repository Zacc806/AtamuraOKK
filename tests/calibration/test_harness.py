"""Tests for the calibration comparison/verdict."""

from __future__ import annotations

from AtamuraOKK.calibration.harness import compare
from AtamuraOKK.calibration.xlsx_loader import HumanCall
from AtamuraOKK.scoring.base import CriterionScore, ScoreResult

MAX_TOTAL = 50
THRESHOLD = 75


def _human(deal: int, total: int, crit1: int) -> HumanCall:
    """A human-scored call with a single tracked criterion (#1)."""
    return HumanCall(
        manager="m",
        reviewer="r",
        crm_deal_id=deal,
        crm_url=f"/deal/details/{deal}/",
        raw_total=total,
        per_criterion={1: crit1},
    )


def _ai(pct: float, *, passed: bool, crit1: int) -> ScoreResult:
    """An AI score result with a single tracked criterion (#1)."""
    return ScoreResult(
        rubric_version="okk_meeting_v1",
        total_score=int(pct / 2),
        max_total=MAX_TOTAL,
        score_pct=pct,
        passed=passed,
        criteria=[CriterionScore(id=1, block="b", name="n", score=crit1, max_score=1)],
        client_agreed_meeting=False,
        manager_tone="нейтральный",
        red_flags=[],
        summary="",
        language="ru",
        provider="groq",
        model="m",
    )


def test_strong_agreement_passes() -> None:
    """Close totals and identical pass/fail decisions yield a PASS verdict."""
    human = [_human(1, 40, 1), _human(2, 45, 1), _human(3, 20, 0)]
    ai = {
        1: _ai(82, passed=True, crit1=1),
        2: _ai(88, passed=True, crit1=1),
        3: _ai(42, passed=False, crit1=0),
    }
    report = compare(human, ai, max_total=MAX_TOTAL, pass_threshold=THRESHOLD)
    assert report.n == 3
    assert report.passfail["kappa"] == 1.0
    assert report.per_criterion_kappa[1] == 1.0
    assert report.verdict == "PASS"


def test_no_matches_fails() -> None:
    """No overlapping deal ids yields an n=0 FAIL report."""
    human = [_human(1, 40, 1)]
    ai = {99: _ai(80, passed=True, crit1=1)}
    report = compare(human, ai, max_total=MAX_TOTAL, pass_threshold=THRESHOLD)
    assert report.n == 0
    assert report.verdict == "FAIL"


def test_pass_fail_disagreement_lowers_verdict() -> None:
    """Systematic pass/fail disagreement is not a PASS."""
    human = [_human(1, 40, 1), _human(2, 20, 0)]  # pass, fail
    ai = {
        1: _ai(40, passed=False, crit1=0),  # AI fails what human passed
        2: _ai(90, passed=True, crit1=1),  # AI passes what human failed
    }
    report = compare(human, ai, max_total=MAX_TOTAL, pass_threshold=THRESHOLD)
    assert report.verdict != "PASS"
