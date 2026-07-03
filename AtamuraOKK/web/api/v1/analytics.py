"""Live "Моя Аналитика" read-through over the Bitrix TM funnel.

Like :mod:`day`, this reads **straight through to Bitrix** per request (short TTL
cache) rather than OKK's Postgres — it is a real-time, manager-owned view of the
deal pipeline (cat 24), activities and telephony. It reuses ``day``'s
cache-backed stage-history helpers so a period pulled for /day is not re-pulled
here.

Four blocks, each independently resilient (a failing sub-read degrades that block
to ``status="not_available"`` rather than failing the whole view):

* **funnel**   — leads → qualified → meeting-set → arrived counts + overall CR +
  trailing-N-months CR trend, plus two leakage bars straight from Bitrix:
  «Недозвон» (leads that entered a no-answer stage) and «Закрыто (отказ)» (deals
  closed with fail semantics). «Купили» is the cat-2 booking (attributed back).
* **tasks**    — activity counts by deadline: total / closed / overdue / open.
  «Закрыто в срок» needs a completion timestamp the count API does not expose →
  None for now.
* **meetings** — назначено / дошли / переназначились / недошли (купили None).
* **calls**    — talk time / completed / no-answer / incoming from telephony.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.settings import settings
from AtamuraOKK.web.api.v1 import day, okk
from AtamuraOKK.web.api.v1.schemas import (
    AnalyticsCalls,
    AnalyticsFunnel,
    AnalyticsMeetings,
    AnalyticsTasks,
    AnalyticsTrendPoint,
    AnalyticsView,
    FunnelReason,
    FunnelStage,
)

# Bitrix CALL_TYPE: 1 = outbound, 2 = inbound (ingestion/mapping.py).
_INCOMING_CALL_TYPE = "2"

# (uid, period_label) -> (monotonic expiry, AnalyticsView).
_cache: dict[tuple[int, str], tuple[float, AnalyticsView]] = {}


def _pct(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 1) if denominator else None


def _month_window(year: int, month: int) -> tuple[datetime, datetime]:
    """[first day of month, first day of next month) in the report timezone."""
    tz = ZoneInfo(settings.report_timezone)
    start = datetime(year, month, 1, tzinfo=tz)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end = datetime(year, month + 1, 1, tzinfo=tz)
    return start, end


async def _leads(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
) -> int:
    """Deals created in the period and owned by the manager (funnel mouth)."""
    return await day.count_list(
        bx,
        "crm.deal.list",
        {
            "CATEGORY_ID": settings.companion_tm_category_id,
            "ASSIGNED_BY_ID": uid,
            ">=DATE_CREATE": start.date().isoformat(),
            "<DATE_CREATE": end.date().isoformat(),
        },
    )


# Bucket key for lost deals whose reason field is empty (single, unresolvable).
_UNSPECIFIED_REASON = "Не указана"
# field name -> (monotonic expiry, {enum value id: label}). The deal's enum list
# changes rarely, so it is cached per-process for the analytics TTL.
_enum_label_cache: dict[str, tuple[float, dict[str, str]]] = {}


async def _deal_enum_labels(bx: BitrixClient, field: str) -> dict[str, str]:
    """``{enum value id: label}`` for a deal enumeration UF field, cached.

    Resolved from ``crm.deal.fields`` (the field's ``items``). Best-effort: a
    Bitrix failure yields ``{}`` (breakdown then falls back to raw ids) rather
    than failing the funnel, and is not cached so the next call retries.
    """
    hit = _enum_label_cache.get(field)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        fields = await bx.call("crm.deal.fields")
    except BitrixError:
        return {}
    meta = fields.get(field) if isinstance(fields, dict) else None
    labels: dict[str, str] = {}
    for item in (meta or {}).get("items") or []:
        fid, value = item.get("ID"), item.get("VALUE")
        if fid is not None and value is not None:
            labels[str(fid)] = str(value)
    expiry = time.monotonic() + settings.companion_analytics_cache_ttl_seconds
    _enum_label_cache[field] = (expiry, labels)
    return labels


def _reasons_from_counts(
    counts: dict[str, int],
    labels: dict[str, str],
) -> list[FunnelReason]:
    """Turn ``{enum id: count}`` into labelled reasons, largest first.

    The empty-string key is the «не указана» bucket; an id missing from ``labels``
    falls back to the raw id as its own label (so an unresolved value still shows).
    """
    reasons: list[FunnelReason] = []
    for raw, n in counts.items():
        if raw == "":
            reasons.append(FunnelReason(label=_UNSPECIFIED_REASON, count=n))
        else:
            reasons.append(
                FunnelReason(label=labels.get(raw, raw), count=n, reason_id=raw),
            )
    reasons.sort(key=lambda r: (-r.count, r.label))
    return reasons


async def _closed_lost(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
) -> tuple[int, list[FunnelReason] | None]:
    """Deals closed unsuccessfully (fail semantics) in the period, with a reason split.

    ``STAGE_SEMANTIC_ID='F'`` selects the failure/«отказ» stages regardless of
    their portal-specific STATUS_IDs; scoped by ``CLOSEDATE`` to the period. Lost
    deals rest in cat 24 (only WON ones are moved to the sales funnel), so a
    snapshot read is correct here — no stage history needed.

    Returns ``(total, breakdown)``. When ``companion_closed_reason_field`` is set,
    pages the lost deals carrying that enum field and groups them by отказ-причина
    (labels from :func:`_deal_enum_labels`); otherwise a cheap envelope count with
    ``breakdown=None``. The reason is single-select, so the breakdown counts sum to
    the total.
    """
    filter_ = {
        "CATEGORY_ID": settings.companion_tm_category_id,
        "ASSIGNED_BY_ID": uid,
        "STAGE_SEMANTIC_ID": "F",
        ">=CLOSEDATE": start.date().isoformat(),
        "<CLOSEDATE": end.date().isoformat(),
    }
    field = settings.companion_closed_reason_field
    if not field:
        return await day.count_list(bx, "crm.deal.list", filter_), None

    total = 0
    counts: dict[str, int] = {}
    async for deal in bx.list(
        "crm.deal.list",
        {"filter": filter_, "select": ["ID", field]},
    ):
        total += 1
        raw = deal.get(field)
        values = raw if isinstance(raw, list) else [raw]
        picked = [str(v) for v in values if v not in (None, "", 0, "0")]
        if not picked:
            counts[""] = counts.get("", 0) + 1
        for v in picked:
            counts[v] = counts.get(v, 0) + 1

    if not counts:  # nothing lost — skip the (cached) crm.deal.fields round-trip
        return total, []
    labels = await _deal_enum_labels(bx, field)
    return total, _reasons_from_counts(counts, labels)


async def _funnel(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
) -> AnalyticsFunnel:
    """Stage counts + overall CR + monthly CR trend for the funnel block.

    Besides the progression Лиды→…→Купили, two **leakage** bars come straight from
    Bitrix: «Недозвон» (distinct leads that entered a no-answer stage in the
    period) and «Закрыто (отказ)» (deals closed unsuccessfully — fail semantics —
    in the period). They are not sequential steps, so the UI shows them as a share
    of leads, not a stage-to-stage CR.
    """
    try:
        leads = await _leads(bx, uid, start, end)
        no_answer = (
            await day.stage_entrants_by_assignee(
                bx, settings.companion_no_answer_stage_ids, start, end
            )
        ).get(uid, 0)
        qualified = (
            await day.stage_outcomes_by_assignee(
                bx, settings.companion_qualified_stage_id, start, end
            )
        )[0].get(uid, 0)
        meeting_set = (
            await day.stage_outcomes_by_assignee(
                bx, settings.companion_meeting_set_stage_id, start, end
            )
        )[0].get(uid, 0)
        arrived = (await day.conducted_meetings_by_tm(bx, start, end)).get(uid, 0)
        bought = (await day.sold_deals_by_tm(bx, start, end)).get(uid, 0)
        closed_lost, closed_lost_reasons = await _closed_lost(bx, uid, start, end)
    except BitrixError:
        return AnalyticsFunnel(status="not_available")

    stages = [
        FunnelStage(key="leads", label="Лиды", count=leads),
        FunnelStage(key="no_answer", label="Недозвон", count=no_answer),
        FunnelStage(key="qualified", label="Квалифицированы", count=qualified),
        FunnelStage(key="meeting_set", label="Назначена встреча", count=meeting_set),
        FunnelStage(key="arrived", label="Дошли", count=arrived),
        FunnelStage(key="bought", label="Купили", count=bought),
        FunnelStage(
            key="closed_lost",
            label="Закрыто (отказ)",
            count=closed_lost,
            breakdown=closed_lost_reasons or None,
        ),
    ]
    any_activity = bool(
        leads or qualified or meeting_set or arrived or bought or no_answer
        or closed_lost
    )
    # The monthly CR trend is a heavy multi-month Bitrix fan-out — it is served
    # lazily by get_cr_trend() / the /analytics/cr-trend endpoint so its cost can
    # never delay (or 504) the four main blocks. ``trend`` stays empty here.
    return AnalyticsFunnel(
        status="live" if any_activity else "not_available",
        stages=stages,
        overall_cr_pct=_pct(arrived, leads),
    )


async def _cr_trend(
    bx: BitrixClient,
    uid: int,
    period_start: datetime,
) -> list[AnalyticsTrendPoint]:
    """Trailing-N-months conversion (arrived ÷ leads), oldest→newest.

    ``arrived`` per month comes from ONE combined WON pull over the whole window
    (:func:`day.won_by_month_for_tm`) rather than a pull per month; ``leads`` per
    month are cheap envelope counts. The whole trend degrades to empty (not a
    failure) on a Bitrix error, since it is a best-effort secondary panel.
    """
    months = max(1, settings.companion_analytics_trend_months)
    year, month = period_start.year, period_start.month
    windows: list[tuple[int, int]] = []
    for _ in range(months):
        windows.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    windows.reverse()

    oldest_start, _ = _month_window(*windows[0])
    _, newest_end = _month_window(*windows[-1])
    try:
        arrived_by_month = await day.won_by_month_for_tm(
            bx, uid, oldest_start, newest_end
        )
        points: list[AnalyticsTrendPoint] = []
        for y, m in windows:
            label = f"{y:04d}-{m:02d}"
            m_start, m_end = _month_window(y, m)
            leads = await _leads(bx, uid, m_start, m_end)
            cr = _pct(arrived_by_month.get(label, 0), leads)
            points.append(AnalyticsTrendPoint(period=label, cr_pct=cr))
    except BitrixError:
        return []
    return points


# (uid, period_label) -> (monotonic expiry, trend points). Separate from the main
# view's cache so the heavy trend caches independently.
_trend_cache: dict[tuple[int, str], tuple[float, list[AnalyticsTrendPoint]]] = {}


async def get_cr_trend(
    bitrix_user_id: int,
    period: str | None,
) -> list[AnalyticsTrendPoint]:
    """Lazy CR trend for a manager — the heavy panel, loaded after the blocks."""
    start, _end, label = okk.parse_period(period)
    cache_key = (bitrix_user_id, label)
    hit = _trend_cache.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        async with BitrixClient() as bx:
            points = await _cr_trend(bx, bitrix_user_id, start)
    except BitrixError:
        return []
    expiry = time.monotonic() + settings.companion_analytics_cache_ttl_seconds
    _trend_cache[cache_key] = (expiry, points)
    return points


async def _tasks(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
) -> AnalyticsTasks:
    """Activity counts for the period, split by completion and deadline."""
    now = datetime.now(tz=ZoneInfo(settings.report_timezone))
    s_iso, e_iso = start.isoformat(), end.isoformat()

    async def _count(extra: dict[str, Any]) -> int:
        return await day.count_list(
            bx,
            "crm.activity.list",
            {"RESPONSIBLE_ID": uid, ">=DEADLINE": s_iso, "<DEADLINE": e_iso, **extra},
        )

    try:
        closed = await _count({"COMPLETED": "Y"})
        # Overdue: not done, deadline in [start, min(end, now)). Open: not done,
        # deadline in [max(start, now), end). Clamp so a past/future period is sane.
        overdue_end = min(end, now)
        overdue = 0
        if overdue_end > start:
            overdue = await day.count_list(
                bx,
                "crm.activity.list",
                {
                    "RESPONSIBLE_ID": uid,
                    "COMPLETED": "N",
                    ">=DEADLINE": s_iso,
                    "<DEADLINE": overdue_end.isoformat(),
                },
            )
        open_start = max(start, now)
        pending = 0
        if open_start < end:
            pending = await day.count_list(
                bx,
                "crm.activity.list",
                {
                    "RESPONSIBLE_ID": uid,
                    "COMPLETED": "N",
                    ">=DEADLINE": open_start.isoformat(),
                    "<DEADLINE": e_iso,
                },
            )
    except BitrixError:
        return AnalyticsTasks(status="not_available")

    total = closed + overdue + pending
    return AnalyticsTasks(
        status="live" if total else "not_available",
        total=total,
        closed=closed,
        closed_on_time=None,
        overdue=overdue,
        pending=pending,
    )


async def _meetings(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
) -> AnalyticsMeetings:
    """Meeting outcomes for the period from stage history."""
    try:
        set_distinct, rebooked = await day.stage_outcomes_by_assignee(
            bx, settings.companion_meeting_set_stage_id, start, end
        )
        no_show = (
            await day.stage_outcomes_by_assignee(
                bx, settings.companion_no_show_stage_id, start, end
            )
        )[0].get(uid, 0)
        arrived = (await day.conducted_meetings_by_tm(bx, start, end)).get(uid, 0)
        bought = (await day.sold_deals_by_tm(bx, start, end)).get(uid, 0)
    except BitrixError:
        return AnalyticsMeetings(status="not_available")

    meetings_set = set_distinct.get(uid, 0)
    rescheduled = rebooked.get(uid, 0)
    live = bool(meetings_set or arrived or no_show or bought)
    return AnalyticsMeetings(
        status="live" if live else "not_available",
        meetings_set=meetings_set,
        arrived=arrived,
        rescheduled=rescheduled,
        no_show=no_show,
        bought=bought,
    )


async def _calls(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
) -> AnalyticsCalls:
    """Telephony stats for the period from a single voximplant pull."""
    talk = completed = no_answer = incoming = 0
    try:
        async for row in bx.list(
            "voximplant.statistic.get",
            {
                "FILTER": {
                    "PORTAL_USER_ID": uid,
                    ">=CALL_START_DATE": start.isoformat(),
                    "<CALL_START_DATE": end.isoformat(),
                },
                "ORDER": {"CALL_START_DATE": "ASC"},
            },
        ):
            if row.get("CALL_FAILED_CODE") == settings.ingest_success_code:
                completed += 1
                talk += int(row.get("CALL_DURATION") or 0)
            else:
                no_answer += 1
            if str(row.get("CALL_TYPE")) == _INCOMING_CALL_TYPE:
                incoming += 1
    except BitrixError:
        return AnalyticsCalls(status="not_available")

    live = bool(completed or no_answer or incoming)
    return AnalyticsCalls(
        status="live" if live else "not_available",
        talk_time_sec=talk,
        completed=completed,
        no_answer=no_answer,
        incoming=incoming,
    )


async def get_analytics(
    session: AsyncSession,
    bitrix_user_id: int,
    period: str | None,
) -> AnalyticsView:
    """Live Моя Аналитика for a manager (Bitrix user id) in a YYYY-MM period."""
    start, end, label = okk.parse_period(period)
    cache_key = (bitrix_user_id, label)
    hit = _cache.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    manager = await day.manager_ref(session, bitrix_user_id)
    try:
        async with BitrixClient() as bx:
            # Cold fan-out is heavy (dept-wide stage-history + attribution). Warm
            # the shared caches the funnel/meetings need CONCURRENTLY, overlapping
            # the independent telephony pull — roughly halves wall-time vs running
            # the blocks in series (the funnel/meetings then read warm caches). The
            # Bitrix client self-throttles on rate limits, so concurrency is safe.
            results = await asyncio.gather(
                day.stage_outcomes_by_assignee(
                    bx, settings.companion_qualified_stage_id, start, end
                ),
                day.stage_outcomes_by_assignee(
                    bx, settings.companion_meeting_set_stage_id, start, end
                ),
                day.stage_outcomes_by_assignee(
                    bx, settings.companion_no_show_stage_id, start, end
                ),
                day.conducted_meetings_by_tm(bx, start, end),
                day.sold_deals_by_tm(bx, start, end),
                day.stage_entrants_by_assignee(
                    bx, settings.companion_no_answer_stage_ids, start, end
                ),
                _calls(bx, bitrix_user_id, start, end),
                return_exceptions=True,
            )
            calls_res = results[6]
            calls = (
                calls_res
                if isinstance(calls_res, AnalyticsCalls)
                else AnalyticsCalls(status="not_available")
            )
            funnel = await _funnel(bx, bitrix_user_id, start, end)
            tasks = await _tasks(bx, bitrix_user_id, start, end)
            meetings = await _meetings(bx, bitrix_user_id, start, end)
    except BitrixError as exc:
        logger.warning(
            "Analytics Bitrix read failed for {uid}: {e}",
            uid=bitrix_user_id,
            e=exc,
        )
        return AnalyticsView(manager=manager, period=label)

    view = AnalyticsView(
        manager=manager,
        period=label,
        funnel=funnel,
        tasks=tasks,
        meetings=meetings,
        calls=calls,
    )
    expiry = time.monotonic() + settings.companion_analytics_cache_ttl_seconds
    _cache[cache_key] = (expiry, view)
    return view
