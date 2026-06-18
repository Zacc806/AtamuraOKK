"""Aggregate scored calls in a time window into structured report data."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from AtamuraOKK.db.session import session_scope
from AtamuraOKK.scoring.rubric import load_rubric
from AtamuraOKK.settings import settings

Half = str  # "morning" | "afternoon"


@dataclass
class CallRow:
    """One scored call in the window."""

    call_id: int
    percent: float
    zone: str
    summary: str
    strengths: str
    growth_zone: str
    training_recommendation: str
    target_status: str
    sentiment_customer: str
    red_flags: list[str]
    call_type: str
    is_qualification_call: bool


@dataclass
class ManagerReport:
    """A manager's results in the window."""

    name: str
    department: str | None
    n_calls: int
    avg_percent: float
    zone: str
    calls: list[CallRow]


@dataclass
class CriterionStat:
    """Team-level performance on one checklist criterion."""

    criterion_id: int
    block_name: str
    criterion_text: str
    avg_pct_of_max: float
    scored: int


@dataclass
class ReportData:
    """Everything the writer/renderer need for one half-day report."""

    half: Half
    date_label: str
    window_start: datetime
    window_end: datetime
    n_scored: int
    avg_percent: float
    zones: dict[str, int]
    targets: dict[str, int]
    n_flagged: int
    managers: list[ManagerReport]
    weakest_criteria: list[CriterionStat]
    flagged: list[CallRow] = field(default_factory=list)
    # Non-qualification calls excluded from the score (reminders/vendor/internal/…).
    n_excluded: int = 0
    excluded_by_type: dict[str, int] = field(default_factory=dict)


def compute_window(day: date_cls, half: Half) -> tuple[datetime, datetime, str]:
    """Return (start, end, human label) for a half-day in the report timezone."""
    tz = ZoneInfo(settings.report_timezone)
    midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
    if half == "morning":
        start = midnight
        end = midnight + timedelta(hours=settings.report_split_hour)
        label = (
            f"{day.isoformat()}, первая половина дня "
            f"(до {settings.report_split_hour}:00)"
        )
    else:
        start = midnight + timedelta(hours=settings.report_split_hour)
        end = midnight + timedelta(hours=settings.report_day_end_hour)
        label = (
            f"{day.isoformat()}, вторая половина дня "
            f"({settings.report_split_hour}:00–{settings.report_day_end_hour}:00)"
        )
    return start, end, label


def _flags(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return list(value) if isinstance(value, list) else []


def _call_row(r: Any) -> CallRow:
    return CallRow(
        call_id=r.call_id,
        percent=float(r.percent) if r.percent is not None else 0.0,
        zone=r.zone or "risk",
        summary=r.summary or "",
        strengths=r.strengths or "",
        growth_zone=r.growth_zone or "",
        training_recommendation=r.training_recommendation or "",
        target_status=r.target_status or "неясно",
        sentiment_customer=r.sentiment_customer or "нейтральный",
        red_flags=_flags(r.red_flags),
        call_type=getattr(r, "call_type", None) or "другое",
        is_qualification_call=getattr(r, "is_qualification_call", None) is not False,
    )


async def aggregate_window(
    start: datetime, end: datetime, half: Half, label: str
) -> ReportData:
    """Build :class:`ReportData` for scored calls in ``[start, end)``."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT * FROM call_scores_latest "
                    "WHERE started_at >= :start AND started_at < :end "
                    "ORDER BY percent DESC",
                ),
                {"start": start, "end": end},
            )
        ).all()
        crit_rows = (
            await session.execute(
                text(
                    "SELECT cl.criterion_id, cl.block_name, cl.criterion_text, "
                    "ROUND(AVG(cl.percent_of_max),1) avg_pct, COUNT(*) scored "
                    "FROM call_criteria_latest cl "
                    "JOIN call_scores_latest cs ON cs.call_id = cl.call_id "
                    "WHERE cl.started_at >= :start AND cl.started_at < :end "
                    "AND cs.is_qualification_call IS NOT FALSE "
                    "GROUP BY 1,2,3 ORDER BY avg_pct ASC LIMIT 6",
                ),
                {"start": start, "end": end},
            )
        ).all()

    # Score only genuine qualification calls; the rest are excluded + summarized.
    qual_rows = [
        r for r in rows if getattr(r, "is_qualification_call", None) is not False
    ]
    excluded = [r for r in rows if getattr(r, "is_qualification_call", None) is False]
    excluded_by_type = _count(
        [_call_row(r) for r in excluded],
        lambda c: c.call_type,
    )

    # Manager-average zones must use the same rubric thresholds as per-call
    # scoring (scoring/worker.py), so retuning the rubric JSON's zones stays
    # consistent across per-call and report-level zones.
    rubric = load_rubric()

    calls = [_call_row(r) for r in qual_rows]
    by_manager: dict[str, list[CallRow]] = {}
    mgr_dept: dict[str, str | None] = {}
    for r in qual_rows:
        name = r.manager_name or "(не определён)"
        by_manager.setdefault(name, []).append(_call_row(r))
        mgr_dept[name] = r.department_name

    managers: list[ManagerReport] = []
    for name, mcalls in by_manager.items():
        avg = round(sum(c.percent for c in mcalls) / len(mcalls), 1)
        managers.append(
            ManagerReport(
                name=name,
                department=mgr_dept.get(name),
                n_calls=len(mcalls),
                avg_percent=avg,
                zone=rubric.zone_for(avg),
                calls=sorted(mcalls, key=lambda c: c.percent, reverse=True),
            ),
        )
    managers.sort(key=lambda m: m.avg_percent, reverse=True)

    zones = _count(calls, lambda c: c.zone)
    targets = _count(calls, lambda c: c.target_status)
    flagged = [
        c
        for c in calls
        if c.zone == "risk"
        or c.target_status == "нецелевой"
        or c.sentiment_customer == "негативный"
        or c.red_flags
    ]
    avg_percent = round(sum(c.percent for c in calls) / len(calls), 1) if calls else 0.0

    return ReportData(
        half=half,
        date_label=label,
        window_start=start,
        window_end=end,
        n_scored=len(calls),
        avg_percent=avg_percent,
        zones=zones,
        targets=targets,
        n_flagged=len(flagged),
        managers=managers,
        weakest_criteria=[
            CriterionStat(
                criterion_id=cr.criterion_id,
                block_name=cr.block_name,
                criterion_text=cr.criterion_text,
                avg_pct_of_max=float(cr.avg_pct) if cr.avg_pct is not None else 0.0,
                scored=cr.scored,
            )
            for cr in crit_rows
        ],
        flagged=sorted(flagged, key=lambda c: c.percent),
        n_excluded=len(excluded),
        excluded_by_type=excluded_by_type,
    )


def _count(calls: list[CallRow], key: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in calls:
        k = key(c)
        out[k] = out.get(k, 0) + 1
    return out
