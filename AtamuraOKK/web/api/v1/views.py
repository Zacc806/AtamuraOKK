"""Companion-facing endpoints (``/api/v1``).

DTO-typed, guarded by two auth layers (see ``auth``): the shared service
bearer plus a personal user key that carries the role — a *manager* is scoped
to their own data, the *head of sales* sees everything. Manager/department
path params are **Bitrix** ids (see ``schemas``). The pipeline's internal row
ids and status enum never appear in a response.

Call-quality data is strictly read-only. The one writable surface is the
head-tiered ``/users`` access management: a scoped head (office РОП) manages
manager keys for their own department, the global head manages everything and
additionally mints department-scoped head keys. It writes only AtamuraOKK's
own ``companion_users``/``managers``/``departments`` tables, never the
pipeline state or Bitrix (key issuance may *read* Bitrix to resolve a name).
"""

from __future__ import annotations

import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.dependencies import get_db_session
from AtamuraOKK.db.models.companion_user import CompanionUser
from AtamuraOKK.db.models.department import Department
from AtamuraOKK.db.models.enums import CompanionRole
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.web.api.v1 import analytics, day, hygiene, service
from AtamuraOKK.web.api.v1.auth import (
    CompanionIdentity,
    ensure_access_admin,
    ensure_can_view_manager,
    ensure_global_head,
    ensure_head,
    get_companion_identity,
    hash_key,
    manager_department_bitrix_id,
    require_companion_token,
)
from AtamuraOKK.web.api.v1.okk import PeriodError
from AtamuraOKK.web.api.v1.schemas import (
    AnalyticsTrendPoint,
    AnalyticsView,
    AppealCreate,
    AppealReview,
    AppealView,
    CallFeedback,
    CallFeedItem,
    CompanionUserCreate,
    CompanionUserCreated,
    CompanionUserView,
    CriteriaAveragesView,
    DayView,
    DepartmentRef,
    FeedItem,
    HygieneView,
    ManagerScorecard,
    MeetingFeedback,
    MeetingFeedItem,
    MeView,
    RubricView,
    ScoreTrendView,
    TeamOverdueTasks,
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


@router.get(
    "/crm/{entity_type}/{entity_id}/calls",
    response_model=list[CallFeedItem],
    tags=["companion"],
)
async def crm_entity_calls(
    entity_type: str,
    entity_id: int,
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> list[CallFeedItem]:
    """Scored calls attached to a Bitrix CRM card, newest first.

    ``entity_type``/``entity_id`` are the card URL's path segments
    («…/crm/**contact**/details/**429546**/»). Opening a call from Bitrix lands
    on the **contact** card, and calls link to the contact — so contact, deal,
    company and lead cards are all accepted and cross-resolved through Bitrix so
    the same calls surface whichever card is pasted. Scoped to what the caller
    may see (a manager only their own calls, a scoped head only their
    department's), so an unrelated/out-of-scope card returns an empty list.
    """
    if entity_type.lower() not in service.CRM_ENTITY_TYPES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Unknown CRM entity type — expected one of "
                f"{', '.join(service.CRM_ENTITY_TYPES)}."
            ),
        )
    return await service.get_crm_entity_calls(
        session,
        entity_type.lower(),
        entity_id,
        identity,
    )


@router.get(
    "/deals/{deal_id}/calls",
    response_model=list[CallFeedItem],
    tags=["companion"],
)
async def deal_calls(
    deal_id: int,
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> list[CallFeedItem]:
    """Scored calls attached to a Bitrix deal — alias of ``/crm/deal/{id}/calls``."""
    return await service.get_crm_entity_calls(session, "deal", deal_id, identity)


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
    "/teams/{department_id}/overdue-tasks",
    response_model=TeamOverdueTasks,
    tags=["companion"],
)
async def team_overdue_tasks(
    department_id: int,
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> TeamOverdueTasks:
    """РОП «Просроченные задачи»: all past-deadline team tasks, oldest-due first.

    Live from Bitrix over the department's roster (incomplete activities whose
    deadline is already in the past). Head-scoped like the team summary: global
    head, or the department's own РОП.
    """
    ensure_head(identity, department_id)
    tasks = await service.get_team_overdue_tasks(session, department_id)
    if tasks is None:
        raise HTTPException(status_code=404, detail="Department not found.")
    return tasks


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
    date: str | None = Query(
        default=None,
        description=(
            "YYYY-MM-DD; default today. Scopes «Важные цифры дня» to a past day "
            "so a manager can review earlier days' results."
        ),
    ),
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> DayView:
    """Мой день: live "кому звонить" + meeting/no-answer/cooling stats + money.

    Reads straight through to the Bitrix TM funnel (cat-0 deals owned by this
    manager). ``data_ready=False`` when there's no live pipeline yet. ``date``
    reruns the «Важные цифры дня» tiles for a past day; the queues stay current.
    """
    await ensure_can_view_manager(session, identity, manager_id)
    try:
        return await day.get_day(session, manager_id, period, date)
    except PeriodError as exc:
        raise _bad_period(exc) from exc


@router.get(
    "/managers/{manager_id}/analytics",
    response_model=AnalyticsView,
    tags=["companion"],
)
async def manager_analytics(
    manager_id: int,
    period: str | None = Query(
        default=None,
        description=(
            "YYYY-MM (month), YYYY-MM-DD (day) or "
            "YYYY-MM-DD..YYYY-MM-DD (inclusive range, e.g. a week); "
            "default current month"
        ),
    ),
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> AnalyticsView:
    """Моя Аналитика: funnel/CR + tasks + meetings + calls for a manager in a period.

    Live read-through to the Bitrix TM funnel (stage history), activities and
    telephony. Each block carries its own ``status`` so the cabinet badges them
    independently; fields are null (UI "—") when their source could not be read.
    """
    await ensure_can_view_manager(session, identity, manager_id)
    try:
        return await analytics.get_analytics(session, manager_id, period)
    except PeriodError as exc:
        raise _bad_period(exc) from exc


@router.get(
    "/managers/{manager_id}/analytics/cr-trend",
    response_model=list[AnalyticsTrendPoint],
    tags=["companion"],
)
async def manager_cr_trend(
    manager_id: int,
    period: str | None = Query(default=None, description="YYYY-MM; default current"),
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> list[AnalyticsTrendPoint]:
    """Lazy CR trend (trailing months) for the analytics funnel — loaded separately.

    Split from `/analytics` because the per-month conducted-meeting attribution is
    a heavy Bitrix fan-out; isolating it keeps the four main blocks fast while the
    trend fills in (or degrades to empty) on its own.
    """
    await ensure_can_view_manager(session, identity, manager_id)
    try:
        return await analytics.get_cr_trend(manager_id, period)
    except PeriodError as exc:
        raise _bad_period(exc) from exc


@router.get(
    "/managers/{manager_id}/hygiene",
    response_model=HygieneView,
    tags=["companion"],
)
async def manager_hygiene(
    manager_id: int,
    period: str | None = Query(
        default=None,
        description=(
            "YYYY-MM (month), YYYY-MM-DD (day) or "
            "YYYY-MM-DD..YYYY-MM-DD (inclusive range, e.g. a week); "
            "default current month"
        ),
    ),
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> HygieneView:
    """ОКК · Гигиена CRM: five discipline criteria for a manager in a period.

    Live read-through to Bitrix (open deals, activities). Each criterion carries
    its own ``status`` so the cabinet badges it independently; ``pct`` is null
    (UI «нет данных») when its source is unconfigured or could not be read.
    """
    await ensure_can_view_manager(session, identity, manager_id)
    try:
        return await hygiene.get_hygiene(session, manager_id, period)
    except PeriodError as exc:
        raise _bad_period(exc) from exc


@router.get(
    "/managers/{manager_id}/criteria",
    response_model=CriteriaAveragesView,
    tags=["companion"],
)
async def manager_criteria(
    manager_id: int,
    period: str | None = Query(
        default=None,
        description=(
            "YYYY-MM (month), YYYY-MM-DD (day) or "
            "YYYY-MM-DD..YYYY-MM-DD (range); default current month"
        ),
    ),
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> CriteriaAveragesView:
    """Балл ОКК: average score per rubric criterion over целевые qual calls."""
    await ensure_can_view_manager(session, identity, manager_id)
    try:
        return await service.get_criteria_averages(session, manager_id, period)
    except PeriodError as exc:
        raise _bad_period(exc) from exc


@router.get(
    "/managers/{manager_id}/score-trend",
    response_model=ScoreTrendView,
    tags=["companion"],
)
async def manager_score_trend(
    manager_id: int,
    bucket: str = Query(default="day", description="day | week | month"),
    anchor: str | None = Query(
        default=None,
        description="YYYY-MM-DD; end of the trailing window (default today)",
    ),
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> ScoreTrendView:
    """Динамика: average ОКК percent per day/week/month over a trailing window."""
    await ensure_can_view_manager(session, identity, manager_id)
    try:
        return await service.get_score_trend(session, manager_id, bucket, anchor)
    except PeriodError as exc:
        raise _bad_period(exc) from exc


# --- Appeals (апелляции) -----------------------------------------------------
# A manager disputes specific criteria of a call's ОКК score; their department
# head confirms the ones the manager was right about, each awarded full marks,
# and the corrected percent the read layer then prefers is recomputed
# automatically (see ``service.review_appeal`` / ``service._score_overrides``).
# Manager-initiated, head-resolved; writes only AtamuraOKK's own ``appeals``
# table.


@router.post(
    "/calls/{call_id}/appeal",
    response_model=AppealView,
    status_code=status.HTTP_201_CREATED,
    tags=["companion"],
)
async def file_call_appeal(
    call_id: int,
    payload: AppealCreate,
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> AppealView:
    """Подать апелляцию: a manager flags their call's ОКК score for РОП re-check.

    Only the manager who made the call may appeal it (a head re-checks, never
    appeals). One pending appeal per call — a duplicate returns 409.
    """
    ctx = await service.get_call_score_context(session, call_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Call not scored.")
    if (
        identity.role is not CompanionRole.MANAGER
        or identity.bitrix_user_id is None
        or ctx.manager_bitrix_user_id != identity.bitrix_user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the manager who made the call can appeal its score.",
        )
    if await service.get_open_appeal_for_call(session, call_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An appeal for this call is already pending review.",
        )
    valid_ids = await service.valid_criterion_ids(session, call_id)
    unknown = sorted(
        {c.criterion_id for c in payload.disputed_criteria} - valid_ids,
    )
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown criterion ids for this call: {unknown}",
        )
    appeal = await service.create_appeal(
        session,
        call_id=call_id,
        manager_bitrix_user_id=ctx.manager_bitrix_user_id,
        created_by_bitrix_user_id=identity.bitrix_user_id,
        department_bitrix_id=ctx.department_bitrix_id,
        disputed_criteria=[c.model_dump() for c in payload.disputed_criteria],
        reason=payload.reason,
    )
    return await service.view_for_appeal(session, appeal)


@router.get("/appeals", response_model=list[AppealView], tags=["companion"])
async def list_appeals(
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="pending | accepted | rejected; default all",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> list[AppealView]:
    """Appeals visible to the caller, newest first.

    A head sees their scope's appeals (global = all, office РОП = own
    department); a manager sees only their own. Pair with ``?status=pending``
    for the head's review queue.
    """
    if identity.role is CompanionRole.HEAD:
        return await service.list_appeals(
            session,
            department_bitrix_id=identity.department_id,
            status=status_filter,
            limit=limit,
        )
    return await service.list_appeals(
        session,
        manager_bitrix_user_id=identity.bitrix_user_id,
        status=status_filter,
        limit=limit,
    )


@router.post(
    "/appeals/{appeal_id}/review",
    response_model=AppealView,
    tags=["companion"],
)
async def review_appeal(
    appeal_id: int,
    payload: AppealReview,
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> AppealView:
    """РОП verdict: confirm the contested criteria the manager was right about.

    Global head reviews any appeal; an office РОП only their own department's.
    Each confirmed criterion is awarded full marks and the call's score
    recalculates automatically — that corrected percent is what the cabinet shows
    for the call everywhere. The head may also clear red flags the appeal
    resolves (``dismissed_flags``), so they don't contradict a criterion now at
    full marks; flags clear only on an accepted appeal. Confirming nothing
    rejects the appeal and leaves the score and all its red flags unchanged.
    """
    ensure_head(identity)
    appeal = await service.get_appeal(session, appeal_id)
    if appeal is None:
        raise HTTPException(status_code=404, detail="Appeal not found.")
    if (
        identity.department_id is not None
        and appeal.department_bitrix_id != identity.department_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A department head can only review their own department's appeals.",
        )
    disputed_ids = {
        int(c["criterion_id"]) for c in (appeal.disputed_criteria or [])
    }
    invalid = sorted(set(payload.confirmed_criteria) - disputed_ids)
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Cannot confirm criteria not in the appeal: {invalid}",
        )
    reviewed = await service.review_appeal(
        session,
        appeal,
        confirmed_criteria=payload.confirmed_criteria,
        dismissed_flags=payload.dismissed_flags,
        note=payload.note,
        reviewed_by_bitrix_user_id=identity.bitrix_user_id,
    )
    return await service.view_for_appeal(session, reviewed)


# --- Access management (head-tiered) -----------------------------------------
# The global РОП (static ``companion_head_key`` or a dept-NULL head row)
# manages everything: manager keys org-wide plus minting/revoking
# department-scoped head keys (office РОПы). ``department_id`` is required to
# mint a head, so a compromised cabinet session can never mint a *global*
# head, and dept-NULL head rows can't be revoked from the cabinet either —
# the global head stays env/CLI-managed. A scoped head manages only their own
# department's MANAGER keys; issuing one ties the manager to that department
# ("cabinet wins" over Bitrix attribution, see
# ``service.assign_manager_department``).


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


@router.get(
    "/departments",
    response_model=list[DepartmentRef],
    tags=["companion"],
)
async def list_departments(
    _identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> list[DepartmentRef]:
    """Departments (Bitrix id + name) for the office-РОП assignment dropdown.

    Global head only — same gate as minting office-РОП keys; names are
    backfilled from Bitrix so the picker shows offices, not raw ids.
    """
    ensure_global_head(_identity)
    return await service.list_departments(session)


@router.get("/users", response_model=list[CompanionUserView], tags=["companion"])
async def list_companion_users(
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> list[CompanionUserView]:
    """Cabinet users (доступы) the caller administers.

    All rows for the global head; only the own department's manager keys
    for a scoped head.
    """
    ensure_access_admin(identity)
    query = select(CompanionUser).order_by(CompanionUser.id)
    if not identity.is_global_head:
        # Inner joins drop keyless rows and unenriched managers — unattributed
        # keys stay global-head-only, mirroring ensure_can_view_manager.
        query = (
            query.join(
                Manager,
                Manager.bitrix_user_id == CompanionUser.bitrix_user_id,
            )
            .join(Department, Department.id == Manager.department_id)
            .where(
                CompanionUser.role == CompanionRole.MANAGER,
                Department.bitrix_id == identity.department_id,
            )
        )
    users = (await session.scalars(query)).all()
    return [_user_view(u) for u in users]


@router.post(
    "/users",
    response_model=CompanionUserCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["companion"],
)
async def create_companion_key(
    payload: CompanionUserCreate,
    identity: CompanionIdentity = Depends(get_companion_identity),
    session: AsyncSession = Depends(get_db_session),
) -> CompanionUserCreated:
    """Issue a personal key. The raw key is returned ONCE.

    Any head issues manager keys (a scoped head's manager is tied to their
    department); only the global head issues department-scoped head keys.
    ``name`` is optional — omitted, it is resolved from the Bitrix user id
    (OKK's ``managers`` table, else a live ``user.get``).
    """
    ensure_access_admin(identity)
    if payload.role == "head":
        ensure_global_head(identity)
    name = payload.name
    if name is None and payload.bitrix_user_id is not None:
        name = await service.resolve_manager_name(session, payload.bitrix_user_id)
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
        role=CompanionRole(payload.role),
        bitrix_user_id=payload.bitrix_user_id,
        department_id=payload.department_id,
        name=name,
    )
    session.add(user)
    if (
        payload.role == "manager"
        # Both non-None by construction: the payload validator requires a
        # bitrix_user_id for managers, a non-global head always carries a dept.
        and payload.bitrix_user_id is not None
        and identity.department_id is not None
    ):
        await service.assign_manager_department(
            session,
            payload.bitrix_user_id,
            identity.department_id,
            name,
        )
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
    """Deactivate a key (reactivation stays CLI-only).

    The global head revokes manager keys and scoped-head keys; a scoped head
    only their own department's manager keys. Dept-NULL head rows (a global
    head) are never revocable from the cabinet.
    """
    ensure_access_admin(identity)
    user = await session.get(CompanionUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Companion user not found.")
    if identity.is_global_head:
        if CompanionRole(user.role) is CompanionRole.HEAD and (
            user.department_id is None
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Global head keys are managed via env/CLI, not the cabinet.",
            )
    else:
        dept = await manager_department_bitrix_id(session, user.bitrix_user_id)
        if (
            CompanionRole(user.role) is not CompanionRole.MANAGER
            or not identity.can_view_department(dept)
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "A department head can only revoke their own "
                    "department's manager keys."
                ),
            )
    user.active = False
    await session.flush()
    return _user_view(user)
