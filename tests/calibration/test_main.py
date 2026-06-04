"""Tests for the calibration CLI gate: verdict -> exit-code contract (0 = PASS)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

import AtamuraOKK.calibration.__main__ as cli
from AtamuraOKK.calibration.xlsx_loader import HumanCall
from AtamuraOKK.scoring.base import CriterionScore, ScoreResult


def _human(deal: int, total: int, crit1: int) -> HumanCall:
    return HumanCall(
        manager="m",
        reviewer="r",
        crm_deal_id=deal,
        crm_url=f"/deal/details/{deal}/",
        raw_total=total,
        per_criterion={1: crit1},
    )


def _ai(base: float, *, passed: bool, crit1: int) -> ScoreResult:
    return ScoreResult(
        rubric_version="okk_meeting_v1",
        total_score=int(base / 2),
        max_total=50,
        score_pct=base,
        passed=passed,
        criteria=[CriterionScore(id=1, block="b", name="n", score=crit1, max_score=1)],
        call_type="первичная",
        client_agreed_meeting=False,
        manager_tone="нейтральный",
        red_flags=[],
        summary="",
        language="ru",
        provider="anthropic",
        model="m",
        meta={"base_score_pct": base},
    )


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    human: list[HumanCall],
    ai: dict[int, ScoreResult],
) -> None:
    @contextlib.asynccontextmanager
    async def _scope() -> AsyncIterator[None]:
        yield None

    async def _ai_by_deal(_session: Any, *, rubric_version: str) -> dict[int, ScoreResult]:  # noqa: E501
        assert rubric_version == "okk_meeting_v1"
        return ai

    monkeypatch.setattr(cli, "load_human_calls", lambda _path: human)
    monkeypatch.setattr(cli, "session_scope", _scope)
    monkeypatch.setattr(cli, "ai_scores_by_deal", _ai_by_deal)


def _run() -> int:
    return asyncio.run(
        cli.run(xlsx_path="x.xlsx", rubric_version="okk_meeting_v1", pass_threshold=75),
    )


def test_pass_returns_exit_code_0(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PASS verdict exits 0 (deploy gate green)."""
    human = [_human(1, 40, 1), _human(2, 45, 1), _human(3, 20, 0)]
    ai = {
        1: _ai(82, passed=True, crit1=1),
        2: _ai(88, passed=True, crit1=1),
        3: _ai(42, passed=False, crit1=0),
    }
    _patch(monkeypatch, human=human, ai=ai)
    assert _run() == 0


def test_no_matches_returns_exit_code_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """An n=0 FAIL verdict exits 1 (deploy gate blocks)."""
    _patch(monkeypatch, human=[_human(1, 40, 1)], ai={})
    assert _run() == 1
