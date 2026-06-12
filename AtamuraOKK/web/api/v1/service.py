"""Read queries backing the companion API.

Everything is sourced from the ``call_scores_latest`` / ``call_criteria_latest``
views (the read contract) plus the ``managers`` / ``departments`` tables for
identity. Nothing here writes, and nothing exposes the internal status enum.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.db.models.department import Department
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.db.models.meeting import Meeting
from AtamuraOKK.db.models.rubric_version import RubricVersion
from AtamuraOKK.web.api.v1 import okk
from AtamuraOKK.web.api.v1.schemas import (
    CallFeedback,
    CallFeedItem,
    CriterionFeedback,
    DepartmentRef,
    FeedItem,
    ManagerRef,
    ManagerScorecard,
    MeetingCriterionFeedback,
    MeetingFeedback,
    MeetingFeedItem,
    MeetingsScore,
    MoneyAxis,
    OkkScore,
    RubricCriterionView,
    RubricView,
    TeamGroupStats,
    TeamSummary,
)

_ZONES = ("strong", "normal", "borderline", "risk")


def _flags(value: Any) -> list[str]:
    """JSONB list from a raw ``text()`` row, which may arrive as a JSON string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _is_qual(row: Any) -> bool:
    """A row counts toward the score unless explicitly flagged non-qualification."""
    return getattr(row, "is_qualification_call", None) is not False


def _okk_from_rows(rows: Sequence[Any]) -> tuple[OkkScore, dict[str, int], int]:
    """Aggregate qualification-call rows into (OkkScore, zone_distribution, n)."""
    qual = [r for r in rows if _is_qual(r)]
    zone_dist = dict.fromkeys(_ZONES, 0)
    for r in qual:
        zone = r.zone or "risk"
        zone_dist[zone] = zone_dist.get(zone, 0) + 1

    percents = [float(r.percent) for r in qual if r.percent is not None]
    avg = round(sum(percents) / len(percents), 1) if percents else None
    score = OkkScore(score_5=okk.okk_5(avg), percent=avg, zone=okk.zone_for(avg))
    return score, zone_dist, len(qual)


def _meetings_score_from(meetings: Sequence[Meeting]) -> MeetingsScore:
    """Aggregate Meeting rows into the meetings block (pass/pct semantics)."""
    pcts = [float(m.score_pct) for m in meetings if m.score_pct is not None]
    return MeetingsScore(
        meetings_scored=len(meetings),
        avg_score_pct=round(sum(pcts) / len(pcts), 1) if pcts else None,
        passed=sum(1 for m in meetings if m.passed is True),
        failed=sum(1 for m in meetings if m.passed is False),
        needs_human_review=sum(1 for m in meetings if m.needs_human_review),
    )


async def _meetings_for_manager(
    session: AsyncSession,
    bitrix_user_id: int,
    start: datetime,
    end: datetime,
) -> Sequence[Meeting]:
    """A manager's scored meetings in a period (uploader-scoped, like the feed)."""
    return (
        await session.scalars(
            select(Meeting).where(
                Meeting.uploaded_by_bitrix_id == bitrix_user_id,
                Meeting.meeting_at >= start,
                Meeting.meeting_at < end,
            ),
        )
    ).all()


async def _scored_rows_for_manager(
    session: AsyncSession,
    bitrix_user_id: int,
    start: datetime,
    end: datetime,
) -> list[Any]:
    return list(
        (
            await session.execute(
                text(
                    "SELECT percent, zone, is_qualification_call "
                    "FROM call_scores_latest "
                    "WHERE manager_bitrix_user_id = :uid "
                    "AND started_at >= :start AND started_at < :end",
                ),
                {"uid": bitrix_user_id, "start": start, "end": end},
            )
        ).all(),
    )


async def get_manager_ref(
    session: AsyncSession,
    bitrix_user_id: int,
) -> ManagerRef | None:
    """Resolve a Bitrix user id to a ManagerRef, or None if unknown."""
    manager = await session.scalar(
        select(Manager).where(Manager.bitrix_user_id == bitrix_user_id),
    )
    if manager is None:
        return None
    department = (
        await session.get(Department, manager.department_id)
        if manager.department_id
        else None
    )
    return ManagerRef(
        bitrix_user_id=bitrix_user_id,
        name=_full_name(manager),
        department_id=department.bitrix_id if department else None,
        department_name=department.name if department else None,
    )


async def get_department_ref(
    session: AsyncSession,
    department_bitrix_id: int,
) -> DepartmentRef:
    """A DepartmentRef for a Bitrix department id (name=None if not synced yet)."""
    department = await session.scalar(
        select(Department).where(Department.bitrix_id == department_bitrix_id),
    )
    return DepartmentRef(
        bitrix_id=department_bitrix_id,
        name=department.name if department else None,
    )


async def _bitrix_user_name(bitrix_user_id: int) -> str | None:
    """Live read-only ``user.get`` for a manager the pipeline hasn't seen yet.

    Degrades to ``None`` on any Bitrix problem (unknown id, missing ``user``
    scope, webhook unset/unreachable) — the caller decides how to fail.
    """
    try:
        async with BitrixClient() as bx:
            rows = await bx.call("user.get", {"ID": bitrix_user_id})
    except (BitrixError, ValueError):
        return None
    if not rows:
        return None
    parts = [p for p in (rows[0].get("NAME"), rows[0].get("LAST_NAME")) if p]
    return " ".join(parts) or None


async def resolve_manager_name(
    session: AsyncSession,
    bitrix_user_id: int,
) -> str | None:
    """Display name for a Bitrix user id, so key issuance needs only the id.

    Prefers OKK's own ``managers`` table (already enriched from ``user.get``
    by ingestion); falls back to a live Bitrix lookup for new managers.
    """
    manager = await session.scalar(
        select(Manager).where(Manager.bitrix_user_id == bitrix_user_id),
    )
    if manager is not None:
        full = _full_name(manager)
        if full:
            return full
    return await _bitrix_user_name(bitrix_user_id)


async def get_scorecard(
    session: AsyncSession,
    bitrix_user_id: int,
    period: str | None,
) -> ManagerScorecard | None:
    """Per-manager scorecard for a period, or None if the manager is unknown."""
    manager = await session.scalar(
        select(Manager).where(Manager.bitrix_user_id == bitrix_user_id),
    )
    if manager is None:
        return None
    department = (
        await session.get(Department, manager.department_id)
        if manager.department_id
        else None
    )

    start, end, label = okk.parse_period(period)
    rows = await _scored_rows_for_manager(session, bitrix_user_id, start, end)
    score, zone_dist, n = _okk_from_rows(rows)
    meetings = await _meetings_for_manager(session, bitrix_user_id, start, end)

    return ManagerScorecard(
        manager=ManagerRef(
            bitrix_user_id=bitrix_user_id,
            name=_full_name(manager),
            department_id=department.bitrix_id if department else None,
            department_name=department.name if department else None,
        ),
        period=label,
        okk=score,
        calls_scored=n,
        zone_distribution=zone_dist,
        meetings=_meetings_score_from(meetings),
        money=MoneyAxis(),
    )


async def get_calls_feed(
    session: AsyncSession,
    bitrix_user_id: int,
    since: datetime | None,
    limit: int,
) -> list[CallFeedItem]:
    """A manager's scored-call feed, newest first."""
    # :since is bound either way; a NULL parameter makes the lower bound a no-op,
    # so the SQL stays a single static string (no interpolation).
    rows = (
        await session.execute(
            text(
                "SELECT call_id, bitrix_call_id, started_at, percent, zone, "
                "target_status, sentiment_customer, red_flags, call_type, "
                "is_qualification_call, summary "
                "FROM call_scores_latest "
                "WHERE manager_bitrix_user_id = :uid "
                "AND (CAST(:since AS timestamptz) IS NULL "
                "OR started_at >= CAST(:since AS timestamptz)) "
                "ORDER BY started_at DESC NULLS LAST LIMIT :limit",
            ),
            {"uid": bitrix_user_id, "since": since, "limit": limit},
        )
    ).all()
    return [
        CallFeedItem(
            call_id=r.call_id,
            bitrix_call_id=r.bitrix_call_id,
            started_at=r.started_at,
            percent=float(r.percent) if r.percent is not None else None,
            zone=r.zone,
            okk_5=okk.okk_5(float(r.percent) if r.percent is not None else None),
            target_status=r.target_status,
            sentiment_customer=r.sentiment_customer,
            red_flags=_flags(r.red_flags),
            call_type=r.call_type,
            is_qualification_call=_is_qual(r),
            summary=r.summary or "",
        )
        for r in rows
    ]


async def get_call_feedback(
    session: AsyncSession,
    call_id: int,
) -> CallFeedback | None:
    """Full авто-разбор for one call, or None if it has no score."""
    row = (
        await session.execute(
            text("SELECT * FROM call_scores_latest WHERE call_id = :cid"),
            {"cid": call_id},
        )
    ).first()
    if row is None:
        return None

    crit_rows = (
        await session.execute(
            text(
                "SELECT criterion_id, block_name, criterion_text, score, max, "
                "percent_of_max, justification, evidence, recommendation "
                "FROM call_criteria_latest WHERE call_id = :cid "
                "ORDER BY criterion_id",
            ),
            {"cid": call_id},
        )
    ).all()

    percent = float(row.percent) if row.percent is not None else None
    return CallFeedback(
        call_id=row.call_id,
        bitrix_call_id=row.bitrix_call_id,
        manager=ManagerRef(
            bitrix_user_id=row.manager_bitrix_user_id,
            name=row.manager_name,
            department_id=row.department_bitrix_id,
            department_name=row.department_name,
        ),
        started_at=row.started_at,
        duration_sec=row.duration_sec,
        language=row.language,
        percent=percent,
        zone=row.zone,
        okk_5=okk.okk_5(percent),
        target_status=row.target_status,
        sentiment_customer=row.sentiment_customer,
        sentiment_agent=row.sentiment_agent,
        summary=row.summary or "",
        strengths=row.strengths or "",
        growth_zone=row.growth_zone or "",
        training_recommendation=row.training_recommendation or "",
        red_flags=_flags(row.red_flags),
        call_type=row.call_type,
        is_qualification_call=_is_qual(row),
        criteria=[
            CriterionFeedback(
                criterion_id=cr.criterion_id,
                block_name=cr.block_name,
                criterion_text=cr.criterion_text,
                score=float(cr.score) if cr.score is not None else None,
                max=float(cr.max) if cr.max is not None else None,
                percent_of_max=(
                    float(cr.percent_of_max) if cr.percent_of_max is not None else None
                ),
                justification=cr.justification,
                evidence=cr.evidence,
                recommendation=cr.recommendation,
            )
            for cr in crit_rows
        ],
    )


def _meeting_feed_item(meeting: Meeting) -> MeetingFeedItem:
    return MeetingFeedItem(
        meeting_id=meeting.id,
        bitrix_file_id=meeting.bitrix_file_id,
        source=meeting.source,
        name=meeting.name,
        meeting_at=meeting.meeting_at,
        duration_sec=meeting.duration_sec,
        percent=float(meeting.score_pct) if meeting.score_pct is not None else None,
        passed=meeting.passed,
        call_type=meeting.call_type,
        manager_tone=meeting.manager_tone,
        needs_human_review=meeting.needs_human_review,
        red_flags=_flags(meeting.red_flags),
        summary=meeting.summary or "",
    )


async def get_meetings_feed(
    session: AsyncSession,
    bitrix_user_id: int,
    since: datetime | None,
    limit: int,
) -> list[MeetingFeedItem]:
    """A manager's scored-meeting feed (Встречи), newest first.

    Scoped by the uploader: a meeting belongs to whoever dropped the recording
    into the Disk folder.
    """
    stmt = (
        select(Meeting)
        .where(Meeting.uploaded_by_bitrix_id == bitrix_user_id)
        .order_by(Meeting.meeting_at.desc().nulls_last(), Meeting.id.desc())
        .limit(limit)
    )
    if since is not None:
        stmt = stmt.where(Meeting.meeting_at >= since)
    meetings = (await session.scalars(stmt)).all()
    return [_meeting_feed_item(m) for m in meetings]


_FEED_EPOCH = datetime.min.replace(tzinfo=UTC)


async def get_unified_feed(
    session: AsyncSession,
    bitrix_user_id: int,
    since: datetime | None,
    limit: int,
) -> list[FeedItem]:
    """A manager's calls + meetings merged into one kind-tagged feed.

    Whatever a department scores — ТМ calls or ОП meetings — shows up here;
    the cabinet renders by ``kind``. Newest first, undated items last.
    """
    calls = await get_calls_feed(session, bitrix_user_id, since, limit)
    meetings = await get_meetings_feed(session, bitrix_user_id, since, limit)
    items = [FeedItem(kind="call", at=c.started_at, call=c) for c in calls]
    items += [FeedItem(kind="meeting", at=m.meeting_at, meeting=m) for m in meetings]
    items.sort(key=lambda i: i.at or _FEED_EPOCH, reverse=True)
    return items[:limit]


def _meeting_criteria(score: dict[str, Any] | None) -> list[MeetingCriterionFeedback]:
    """Criteria list out of the stored ScoreResult dict (tolerant of gaps)."""
    raw = (score or {}).get("criteria")
    if not isinstance(raw, list):
        return []
    out: list[MeetingCriterionFeedback] = []
    for item in raw:
        if not isinstance(item, dict) or "id" not in item:
            continue
        out.append(
            MeetingCriterionFeedback(
                criterion_id=int(item["id"]),
                block=item.get("block"),
                name=item.get("name"),
                score=float(item["score"]) if item.get("score") is not None else None,
                max=(
                    float(item["max_score"])
                    if item.get("max_score") is not None
                    else None
                ),
                auto=bool(item.get("auto", False)),
            ),
        )
    return out


async def get_meeting_feedback(
    session: AsyncSession,
    meeting_id: int,
) -> MeetingFeedback | None:
    """Full авто-разбор for one meeting, or None if unknown."""
    meeting = await session.get(Meeting, meeting_id)
    if meeting is None:
        return None
    manager = (
        await get_manager_ref(session, meeting.uploaded_by_bitrix_id)
        if meeting.uploaded_by_bitrix_id is not None
        else None
    )
    if manager is None and meeting.uploaded_by_bitrix_id is not None:
        manager = ManagerRef(bitrix_user_id=meeting.uploaded_by_bitrix_id)
    score = meeting.score or {}
    deviations = score.get("script_deviations")
    return MeetingFeedback(
        meeting_id=meeting.id,
        bitrix_file_id=meeting.bitrix_file_id,
        source=meeting.source,
        name=meeting.name,
        manager=manager,
        meeting_at=meeting.meeting_at,
        duration_sec=meeting.duration_sec,
        language=meeting.language,
        rubric_version=meeting.rubric_version,
        percent=float(meeting.score_pct) if meeting.score_pct is not None else None,
        passed=meeting.passed,
        call_type=meeting.call_type,
        manager_tone=meeting.manager_tone,
        needs_human_review=meeting.needs_human_review,
        script_adherence=score.get("script_adherence"),
        script_deviations=(
            [str(d) for d in deviations] if isinstance(deviations, list) else []
        ),
        red_flags=_flags(meeting.red_flags),
        summary=meeting.summary or "",
        criteria=_meeting_criteria(score),
    )


async def get_team_summary(
    session: AsyncSession,
    department_bitrix_id: int,
    period: str | None,
) -> TeamSummary | None:
    """РОП-вид: roster of scorecards + group rollup, or None if dept unknown."""
    department = await session.scalar(
        select(Department).where(Department.bitrix_id == department_bitrix_id),
    )
    if department is None:
        return None

    start, end, label = okk.parse_period(period)
    rows = (
        await session.execute(
            text(
                "SELECT manager_bitrix_user_id, manager_name, percent, zone, "
                "is_qualification_call FROM call_scores_latest "
                "WHERE department_id = :dept "
                "AND started_at >= :start AND started_at < :end",
            ),
            {"dept": department.id, "start": start, "end": end},
        )
    ).all()

    group_score, group_zones, group_n = _okk_from_rows(rows)

    # Meetings tie into the department via the uploader's manager row; rows
    # whose manager is still an unenriched placeholder (department NULL) are
    # invisible here until ensure_managers backfills them.
    meeting_rows = (
        await session.execute(
            select(Meeting, Manager)
            .join(Manager, Meeting.manager_id == Manager.id)
            .where(
                Manager.department_id == department.id,
                Meeting.meeting_at >= start,
                Meeting.meeting_at < end,
            ),
        )
    ).all()

    by_manager: dict[int, list[Any]] = {}
    names: dict[int, str | None] = {}
    for r in rows:
        uid = r.manager_bitrix_user_id
        if uid is None:
            continue
        by_manager.setdefault(uid, []).append(r)
        names[uid] = r.manager_name

    meetings_by_manager: dict[int, list[Meeting]] = {}
    for meeting, mgr in meeting_rows:
        uid = mgr.bitrix_user_id
        meetings_by_manager.setdefault(uid, []).append(meeting)
        names.setdefault(uid, _full_name(mgr))

    roster: list[ManagerScorecard] = []
    for uid in set(by_manager) | set(meetings_by_manager):
        score, zone_dist, n = _okk_from_rows(by_manager.get(uid, []))
        roster.append(
            ManagerScorecard(
                manager=ManagerRef(
                    bitrix_user_id=uid,
                    name=names.get(uid),
                    department_id=department.bitrix_id,
                    department_name=department.name,
                ),
                period=label,
                okk=score,
                calls_scored=n,
                zone_distribution=zone_dist,
                meetings=_meetings_score_from(meetings_by_manager.get(uid, [])),
                money=MoneyAxis(),
            ),
        )

    def _rank(card: ManagerScorecard) -> tuple[bool, float]:
        # Calls percent if any, else the meetings percent — both are 0-100.
        primary = (
            card.okk.percent
            if card.okk.percent is not None
            else card.meetings.avg_score_pct
        )
        return (primary is None, -(primary or 0.0))

    roster.sort(key=_rank)

    return TeamSummary(
        department=DepartmentRef(
            bitrix_id=department_bitrix_id,
            name=department.name,
        ),
        period=label,
        group=TeamGroupStats(
            calls_scored=group_n,
            okk=group_score,
            zone_distribution=group_zones,
            meetings=_meetings_score_from([m for m, _ in meeting_rows]),
        ),
        roster=roster,
    )


def _rubric_view(rv: RubricVersion) -> RubricView:
    """Normalize an active rubric row into the cabinet projection.

    Two definition shapes exist: the ТМ call rubric (``blocks[].criteria[]``,
    crm-sourced criteria excluded — they are not scored) and the ОП meeting
    rubric (flat ``criteria[]`` with block/name/max_score). Tolerant of gaps,
    like ``_meeting_criteria``.
    """
    raw = rv.definition if isinstance(rv.definition, dict) else {}
    criteria: list[RubricCriterionView] = []
    if isinstance(raw.get("blocks"), list):
        name = raw.get("name")
        for block in raw["blocks"]:
            if not isinstance(block, dict):
                continue
            for c in block.get("criteria") or []:
                if not isinstance(c, dict) or "id" not in c:
                    continue
                if c.get("source", "call") == "crm":
                    continue
                criteria.append(
                    RubricCriterionView(
                        criterion_id=int(c["id"]),
                        block=block.get("name"),
                        name=str(c.get("text") or ""),
                        max=float(c.get("max") or 0),
                    ),
                )
        max_total = sum(c.max for c in criteria)
    else:
        name = raw.get("id")
        for c in raw.get("criteria") or []:
            if not isinstance(c, dict) or "id" not in c:
                continue
            criteria.append(
                RubricCriterionView(
                    criterion_id=int(c["id"]),
                    block=c.get("block"),
                    name=str(c.get("name") or ""),
                    max=float(c.get("max_score") or 0),
                ),
            )
        max_total = float(raw.get("max_total_score") or 0) or sum(
            c.max for c in criteria
        )
    return RubricView(
        source=rv.source,
        version=rv.version,
        name=name,
        max_total=max_total,
        criteria=criteria,
    )


async def get_active_rubrics(session: AsyncSession) -> list[RubricView]:
    """The active criteria set per source — each department's own rubric."""
    rows = (
        await session.scalars(select(RubricVersion).where(RubricVersion.active))
    ).all()
    views = [_rubric_view(rv) for rv in rows]
    views.sort(key=lambda v: (v.source != "tm", v.source))
    return views


def _full_name(manager: Manager) -> str | None:
    parts = [p for p in (manager.name, manager.last_name) if p]
    return " ".join(parts) or None
