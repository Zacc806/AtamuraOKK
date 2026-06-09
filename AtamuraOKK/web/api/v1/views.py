"""Companion-facing read endpoints (``/api/v1``).

Read-only, bearer-token-guarded, DTO-typed. Manager/department path params are
**Bitrix** ids (see ``schemas``). The pipeline's internal row ids and status
enum never appear in a response.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.dependencies import get_db_session
from AtamuraOKK.web.api.v1 import day, service
from AtamuraOKK.web.api.v1.auth import require_companion_token
from AtamuraOKK.web.api.v1.okk import PeriodError
from AtamuraOKK.web.api.v1.schemas import (
    CallFeedback,
    CallFeedItem,
    DayView,
    ManagerScorecard,
    TeamSummary,
)

router = APIRouter(dependencies=[Depends(require_companion_token)])


def _bad_period(exc: PeriodError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=str(exc),
    )


@router.get(
    "/managers/{manager_id}/scorecard",
    response_model=ManagerScorecard,
    tags=["companion"],
)
async def manager_scorecard(
    manager_id: int,
    period: str | None = Query(
        default=None,
        description="YYYY-MM; default current month",
    ),
    session: AsyncSession = Depends(get_db_session),
) -> ManagerScorecard:
    """ОКК scorecard for a manager (Bitrix user id) in a period."""
    try:
        card = await service.get_scorecard(session, manager_id, period)
    except PeriodError as exc:
        raise _bad_period(exc) from exc
    if card is None:
        raise HTTPException(status_code=404, detail="Manager not found.")
    return card


@router.get(
    "/managers/{manager_id}/calls",
    response_model=list[CallFeedItem],
    tags=["companion"],
)
async def manager_calls(
    manager_id: int,
    since: datetime | None = Query(
        default=None,
        description="Lower bound on started_at",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> list[CallFeedItem]:
    """A manager's scored-call feed (Звонки), newest first."""
    return await service.get_calls_feed(session, manager_id, since, limit)


@router.get(
    "/calls/{call_id}/feedback",
    response_model=CallFeedback,
    tags=["companion"],
)
async def call_feedback(
    call_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> CallFeedback:
    """Full авто-разбор за 90 сек for one call (AtamuraOKK internal call id)."""
    feedback = await service.get_call_feedback(session, call_id)
    if feedback is None:
        raise HTTPException(status_code=404, detail="Call not scored.")
    return feedback


@router.get(
    "/teams/{department_id}/summary",
    response_model=TeamSummary,
    tags=["companion"],
)
async def team_summary(
    department_id: int,
    period: str | None = Query(
        default=None,
        description="YYYY-MM; default current month",
    ),
    session: AsyncSession = Depends(get_db_session),
) -> TeamSummary:
    """РОП-вид: per-manager roster + group rollup for a department (Bitrix id)."""
    try:
        summary = await service.get_team_summary(session, department_id, period)
    except PeriodError as exc:
        raise _bad_period(exc) from exc
    if summary is None:
        raise HTTPException(status_code=404, detail="Department not found.")
    return summary


@router.get(
    "/managers/{manager_id}/day",
    response_model=DayView,
    tags=["companion"],
)
async def manager_day(
    manager_id: int,
    period: str | None = Query(
        default=None,
        description="YYYY-MM; default current month (money axis only)",
    ),
    session: AsyncSession = Depends(get_db_session),
) -> DayView:
    """Мой день: live "кому звонить" + meeting/no-answer/cooling stats + money.

    Reads straight through to the Bitrix TM funnel (cat-0 deals owned by this
    manager). ``data_ready=False`` when there's no live pipeline yet.
    """
    try:
        return await day.get_day(session, manager_id, period)
    except PeriodError as exc:
        raise _bad_period(exc) from exc
