"""Companion-facing endpoints (``/api/v1``).

DTO-typed, guarded by two auth layers (see ``auth``): the shared service
bearer plus a personal user key that carries the role — a *manager* is scoped
to their own data, the *head of sales* sees everything. Manager/department
path params are **Bitrix** ids (see ``schemas``). The pipeline's internal row
ids and status enum never appear in a response.

Call-quality data is strictly read-only. The one writable surface is the
head-only ``/users`` access management (issue/revoke **manager** keys) — it
writes only AtamuraOKK's own ``companion_users`` table, never the pipeline
or Bitrix (key issuance may *read* Bitrix to resolve the manager's name).
"""

from __future__ import annotations

import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.dependencies import get_db_session
from AtamuraOKK.db.models.companion_user import CompanionUser
from AtamuraOKK.db.models.enums import CompanionRole
from AtamuraOKK.web.api.v1 import day, service
from AtamuraOKK.web.api.v1.auth import (
    CompanionIdentity,
    ensure_can_view_manager,
    ensure_global_head,
    ensure_head,
    get_companion_identity,
    hash_key,
    require_companion_token,
)
from AtamuraOKK.web.api.v1.okk import PeriodError
from AtamuraOKK.web.api.v1.schemas import (
    CallFeedback,
    CallFeedItem,
    CompanionUserCreate,
    CompanionUserCreated,
    CompanionUserView,
    DayView,
    DepartmentRef,
    FeedItem,
    ManagerScorecard,
    MeetingFeedback,
    MeetingFeedItem,
    MeView,
    RubricView,
    TeamSummary,
)

router = APIRouter(dependencies=[Depends(require_companion_token)])


@router.get("/me", response_model=MeView, tags=["companion"])
async def me(
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> MeView:
    """Who am I — role + linked manager profile; the cabinet boots from this."""
    manager = (
        await service.get_manager_ref(session, identity.bitrix_user_id)
        if identity.bitrix_user_id is not None
        else None
    )
    if identity.department_id is not None:
        department = await service.get_department_ref(session, identity.department_id)
    elif manager and manager.department_id is not None:
        department = DepartmentRef(
            bitrix_id=manager.department_id,
            name=manager.department_name,
        )
    else:
        department = None
    return MeView(
        role=identity.role.value,
        bitrix_user_id=identity.bitrix_user_id,
        name=identity.name or (manager.name if manager else None),
        manager=manager,
        department=department,
    )


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
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> ManagerScorecard:
    """ОКК scorecard for a manager (Bitrix user id) in a period."""
    await ensure_can_view_manager(session, identity, manager_id)
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
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> list[CallFeedItem]:
    """A manager's scored-call feed (Звонки), newest first."""
    await ensure_can_view_manager(session, identity, manager_id)
    return await service.get_calls_feed(session, manager_id, since, limit)


@router.get(
    "/managers/{manager_id}/meetings",
    response_model=list[MeetingFeedItem],
    tags=["companion"],
)
async def manager_meetings(
    manager_id: int,
    since: datetime | None = Query(
        default=None,
        description="Lower bound on meeting_at",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> list[MeetingFeedItem]:
    """A manager's scored-meeting feed (Встречи ОП), newest first.

    Meetings are attributed to whoever uploaded the recording to the Disk
    folder, so ``manager_id`` is that uploader's Bitrix user id.
    """
    await ensure_can_view_manager(session, identity, manager_id)
    return await service.get_meetings_feed(session, manager_id, since, limit)


@router.get(
    "/managers/{manager_id}/feed",
    response_model=list[FeedItem],
    tags=["companion"],
)
async def manager_feed(
    manager_id: int,
    since: datetime | None = Query(
        default=None,
        description="Lower bound on started_at / meeting_at",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> list[FeedItem]:
    """Unified Звонки+Встречи feed, kind-tagged, newest first.

    A department's scored items are whatever it produces — ТМ calls or ОП
    meetings — so the cabinet reads one feed and renders by ``kind``.
    """
    await ensure_can_view_manager(session, identity, manager_id)
    return await service.get_unified_feed(session, manager_id, since, limit)


@router.get("/rubrics", response_model=list[RubricView], tags=["companion"])
async def active_rubrics(
    _identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> list[RubricView]:
    """Active criteria set per source ("tm" calls / "op" meetings).

    Org-wide reference data — each department scores against its own rubric;
    the cabinet uses this to render the criteria behind the numbers.
    """
    return await service.get_active_rubrics(session)


@router.get(
    "/meetings/{meeting_id}/feedback",
    response_model=MeetingFeedback,
    tags=["companion"],
)
async def meeting_feedback(
    meeting_id: int,
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> MeetingFeedback:
    """Full авто-разбор for one scored meeting (AtamuraOKK internal id)."""
    feedback = await service.get_meeting_feedback(session, meeting_id)
    if feedback is None:
        raise HTTPException(status_code=404, detail="Meeting not found.")
    await ensure_can_view_manager(
        session,
        identity,
        feedback.manager.bitrix_user_id if feedback.manager else None,
    )
    return feedback


@router.get(
    "/calls/{call_id}/feedback",
    response_model=CallFeedback,
    tags=["companion"],
)
async def call_feedback(
    call_id: int,
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> CallFeedback:
    """Full авто-разбор за 90 сек for one call (AtamuraOKK internal call id)."""
    feedback = await service.get_call_feedback(session, call_id)
    if feedback is None:
        raise HTTPException(status_code=404, detail="Call not scored.")
    await ensure_can_view_manager(session, identity, feedback.manager.bitrix_user_id)
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
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> TeamSummary:
    """РОП-вид: roster + rollup. Global head, or the department's own РОП."""
    ensure_head(identity, department_id)
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
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> DayView:
    """Мой день: live "кому звонить" + meeting/no-answer/cooling stats + money.

    Reads straight through to the Bitrix TM funnel (cat-0 deals owned by this
    manager). ``data_ready=False`` when there's no live pipeline yet.
    """
    await ensure_can_view_manager(session, identity, manager_id)
    try:
        return await day.get_day(session, manager_id, period)
    except PeriodError as exc:
        raise _bad_period(exc) from exc


# --- Access management (global-head-only) -----------------------------------
# The РОП logs in with the static head key (``companion_head_key``) and issues
# personal keys for managers from the cabinet. Only MANAGER rows are managed
# here; head keys (incl. department-scoped office РОПs) stay static/CLI-issued
# so a compromised cabinet session can never mint another head. Scoped heads
# don't manage access at all — keys are org-wide, issuance stays global.


def _user_view(user: CompanionUser) -> CompanionUserView:
    return CompanionUserView(
        id=user.id,
        role=CompanionRole(user.role).value,
        bitrix_user_id=user.bitrix_user_id,
        department_id=user.department_id,
        name=user.name,
        active=user.active,
        created_at=user.created_at,
    )


@router.get("/users", response_model=list[CompanionUserView], tags=["companion"])
async def list_companion_users(
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> list[CompanionUserView]:
    """All cabinet users (доступы) — the head's access-management list."""
    ensure_global_head(identity)
    users = (
        await session.scalars(select(CompanionUser).order_by(CompanionUser.id))
    ).all()
    return [_user_view(u) for u in users]


@router.post(
    "/users",
    response_model=CompanionUserCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["companion"],
)
async def create_manager_key(
    payload: CompanionUserCreate,
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> CompanionUserCreated:
    """Issue a manager's personal key. The raw key is returned ONCE.

    ``name`` is optional — omitted, it is resolved from the Bitrix user id
    (OKK's ``managers`` table, else a live ``user.get``).
    """
    ensure_global_head(identity)
    name = payload.name or await service.resolve_manager_name(
        session,
        payload.bitrix_user_id,
    )
    if name is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Could not resolve a name for this Bitrix user id — "
                "check the id, or pass 'name' explicitly."
            ),
        )
    key = secrets.token_urlsafe(24)
    user = CompanionUser(
        key_sha256=hash_key(key),
        role=CompanionRole.MANAGER,
        bitrix_user_id=payload.bitrix_user_id,
        name=name,
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return CompanionUserCreated(user=_user_view(user), key=key)


@router.post(
    "/users/{user_id}/revoke",
    response_model=CompanionUserView,
    tags=["companion"],
)
async def revoke_companion_user(
    user_id: int,
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> CompanionUserView:
    """Deactivate a manager's key (reactivation/head rows stay CLI-only)."""
    ensure_global_head(identity)
    user = await session.get(CompanionUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Companion user not found.")
    if CompanionRole(user.role) is not CompanionRole.MANAGER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Head keys are managed via the CLI, not the cabinet.",
        )
    user.active = False
    await session.flush()
    return _user_view(user)
