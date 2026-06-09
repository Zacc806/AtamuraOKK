"""Read queries backing the companion API.

Everything is sourced from the ``call_scores_latest`` / ``call_criteria_latest``
views (the read contract) plus the ``managers`` / ``departments`` tables for
identity. Nothing here writes, and nothing exposes the internal status enum.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.models.department import Department
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.web.api.v1 import okk
from AtamuraOKK.web.api.v1.schemas import (
    CallFeedback,
    CallFeedItem,
    CriterionFeedback,
    DepartmentRef,
    ManagerRef,
    ManagerScorecard,
    MoneyAxis,
    OkkScore,
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

    by_manager: dict[int, list[Any]] = {}
    names: dict[int, str | None] = {}
    for r in rows:
        uid = r.manager_bitrix_user_id
        if uid is None:
            continue
        by_manager.setdefault(uid, []).append(r)
        names[uid] = r.manager_name

    roster: list[ManagerScorecard] = []
    for uid, mrows in by_manager.items():
        score, zone_dist, n = _okk_from_rows(mrows)
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
                money=MoneyAxis(),
            ),
        )
    roster.sort(key=lambda m: (m.okk.percent is None, -(m.okk.percent or 0.0)))

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
        ),
        roster=roster,
    )


def _full_name(manager: Manager) -> str | None:
    parts = [p for p in (manager.name, manager.last_name) if p]
    return " ".join(parts) or None
