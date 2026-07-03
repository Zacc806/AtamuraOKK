"""Live «ОКК · Гигиена CRM» read-through over the Bitrix TM funnel.

Like :mod:`day` and :mod:`analytics`, this reads **straight through to Bitrix**
per request (short TTL cache) rather than OKK's Postgres — it measures the
discipline of keeping the deal card in order *after* the call, which lives in the
CRM, not in the scoring tables.

Five criteria, each independently resilient (a failing sub-read degrades that one
criterion to ``status="not_available"`` rather than failing the whole view):

* **statuses**     — «Правильное вписывание статусов». Proxy: share of the
  manager's open TM deals that are NOT stale (had activity within
  ``companion_hygiene_stale_days``). The strict "stage matches the call outcome"
  check needs an ОКК transcript↔stage comparison on the scoring side (not wired);
  a stuck, untouched card is the computable signal that the status is not kept.
* **anketa**       — «Правильное заполнение анкеты». Share of open deals with
  every configured questionnaire field (``companion_anketa_fields``) filled.
  Unconfigured → not_available (we never invent a field list).
* **tasks_set**    — «Постановка дел». Share of open deals that carry at least one
  open (incomplete) activity — i.e. a planned next step, no "abandoned" cards.
* **tasks_on_time**— «Исполнение дел в сроки». Of activities whose deadline has
  already passed in the period, the share that are not left hanging overdue.
* **notes**        — «Примечание по шаблону». Share of completed call activities in
  the period that carry a note (matching ``companion_note_template_marker`` when
  set, else any non-empty note).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.settings import settings
from AtamuraOKK.web.api.v1 import day, okk
from AtamuraOKK.web.api.v1.schemas import HygieneCriterion, HygieneView

# Bitrix CRM owner type id for a deal (crm.activity OWNER_TYPE_ID).
_DEAL_OWNER_TYPE = 2

# (uid, period_label) -> (monotonic expiry, HygieneView).
_cache: dict[tuple[int, str], tuple[float, HygieneView]] = {}


def _pct(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 1) if denominator else None


def _now() -> datetime:
    return datetime.now(tz=ZoneInfo(settings.report_timezone))


def _filled(value: Any) -> bool:
    """A questionnaire field counts as filled unless it is empty/blank."""
    if value is None:
        return False
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return str(value).strip() != ""


def _unavailable(key: str, note: str) -> HygieneCriterion:
    return HygieneCriterion(key=key, status="not_available", note=note)


def _scored(
    key: str,
    numerator: int,
    denominator: int,
    note: str | None = None,
) -> HygieneCriterion:
    return HygieneCriterion(
        key=key,
        status="live",
        pct=_pct(numerator, denominator),
        numerator=numerator,
        denominator=denominator,
        note=note,
    )


async def _open_deals(bx: BitrixClient, uid: int) -> list[dict[str, Any]]:
    """Open TM-funnel deals for the manager, with the anketa fields selected."""
    select = ["ID", "STAGE_ID", "LAST_ACTIVITY_TIME", *settings.companion_anketa_fields]
    return [
        d
        async for d in bx.list(
            "crm.deal.list",
            {
                "filter": {
                    "CATEGORY_ID": settings.companion_tm_category_id,
                    "ASSIGNED_BY_ID": uid,
                    "CLOSED": "N",
                },
                "select": select,
                "order": {"LAST_ACTIVITY_TIME": "ASC"},
            },
            max_items=settings.companion_day_max_scan,
        )
    ]


async def _open_task_deal_ids(bx: BitrixClient, uid: int) -> set[int]:
    """Deal ids that carry at least one open (incomplete) activity for the manager."""
    ids: set[int] = set()
    async for row in bx.list(
        "crm.activity.list",
        {
            "filter": {
                "RESPONSIBLE_ID": uid,
                "COMPLETED": "N",
                "OWNER_TYPE_ID": _DEAL_OWNER_TYPE,
            },
            "select": ["ID", "OWNER_ID"],
        },
        max_items=settings.companion_day_max_scan,
    ):
        owner = row.get("OWNER_ID")
        if owner is not None:
            ids.add(int(owner))
    return ids


def _statuses(deals: list[dict[str, Any]] | None) -> HygieneCriterion:
    """Share of open deals not stale (touched within the stale window)."""
    if deals is None:
        return _unavailable("statuses", "Bitrix недоступен")
    if not deals:
        return _unavailable("statuses", "Нет открытых сделок в работе")
    cutoff = _now() - timedelta(days=settings.companion_hygiene_stale_days)
    maintained = 0
    for d in deals:
        raw = d.get("LAST_ACTIVITY_TIME")
        last: datetime | None = None
        if raw:
            try:
                last = datetime.fromisoformat(str(raw))
            except ValueError:
                last = None
        if last is not None and last >= cutoff:
            maintained += 1
    note = (
        f"Прокси-метрика: открытая сделка без активности дольше "
        f"{settings.companion_hygiene_stale_days} дн. считается зависшей. Строгая "
        "сверка статуса с исходом звонка — на стороне ОКК-скоринга (в плане)."
    )
    return _scored("statuses", maintained, len(deals), note)


def _anketa(deals: list[dict[str, Any]] | None) -> HygieneCriterion:
    """Share of open deals with every configured questionnaire field filled."""
    fields = settings.companion_anketa_fields
    if not fields:
        return _unavailable(
            "anketa",
            "Не задан список полей анкеты (ATAMURAOKK_COMPANION_ANKETA_FIELDS)",
        )
    if deals is None:
        return _unavailable("anketa", "Bitrix недоступен")
    if not deals:
        return _unavailable("anketa", "Нет открытых сделок в работе")
    complete = sum(1 for d in deals if all(_filled(d.get(f)) for f in fields))
    return _scored("anketa", complete, len(deals))


def _tasks_set(
    deals: list[dict[str, Any]] | None,
    deal_ids_with_task: set[int] | None,
) -> HygieneCriterion:
    """Share of open deals that have at least one open activity planned."""
    if deals is None or deal_ids_with_task is None:
        return _unavailable("tasks_set", "Bitrix недоступен")
    if not deals:
        return _unavailable("tasks_set", "Нет открытых сделок в работе")
    with_task = sum(1 for d in deals if int(d["ID"]) in deal_ids_with_task)
    return _scored("tasks_set", with_task, len(deals))


async def _tasks_on_time(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
) -> HygieneCriterion:
    """Of activities already due in the period, the share not left overdue."""
    due_end = min(end, _now())
    if due_end <= start:
        return _unavailable("tasks_on_time", "В периоде ещё нет наступивших сроков")
    try:
        due = await day.count_list(
            bx,
            "crm.activity.list",
            {
                "RESPONSIBLE_ID": uid,
                ">=DEADLINE": start.isoformat(),
                "<DEADLINE": due_end.isoformat(),
            },
        )
        overdue = await day.count_list(
            bx,
            "crm.activity.list",
            {
                "RESPONSIBLE_ID": uid,
                "COMPLETED": "N",
                ">=DEADLINE": start.isoformat(),
                "<DEADLINE": due_end.isoformat(),
            },
        )
    except BitrixError:
        return _unavailable("tasks_on_time", "Bitrix недоступен")
    if not due:
        return _unavailable("tasks_on_time", "Нет дел с наступившим сроком")
    note = (
        "Дело с наступившим сроком, оставшееся незакрытым, считается просроченным. "
        "Строгое «закрыто точно в срок» требует таймстемпа закрытия активности "
        "(его нет в count-API), поэтому закрытое с опозданием тут засчитывается."
    )
    return _scored("tasks_on_time", due - overdue, due, note)


async def _notes(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
) -> HygieneCriterion:
    """Share of completed call activities in the period carrying a note."""
    marker = settings.companion_note_template_marker.strip().lower()
    total = with_note = 0
    try:
        async for row in bx.list(
            "crm.activity.list",
            {
                "filter": {
                    "RESPONSIBLE_ID": uid,
                    "COMPLETED": "Y",
                    "TYPE_ID": settings.companion_call_activity_type_id,
                    ">=CREATED": start.isoformat(),
                    "<CREATED": end.isoformat(),
                },
                "select": ["ID", "DESCRIPTION"],
            },
            max_items=settings.companion_day_max_scan,
        ):
            total += 1
            text = str(row.get("DESCRIPTION") or "").strip()
            if text and (not marker or marker in text.lower()):
                with_note += 1
    except BitrixError:
        return _unavailable("notes", "Bitrix недоступен")
    if not total:
        return _unavailable("notes", "Нет завершённых звонков за период")
    note = (
        f"Примечание засчитывается при наличии шаблонной метки «{marker}»."
        if marker
        else "Засчитывается любое непустое примечание к звонку."
    )
    return _scored("notes", with_note, total, note)


async def get_hygiene(
    session: AsyncSession,
    bitrix_user_id: int,
    period: str | None,
) -> HygieneView:
    """Live CRM-hygiene view for a manager (Bitrix user id) in a period."""
    start, end, label = okk.parse_period(period)
    cache_key = (bitrix_user_id, label)
    hit = _cache.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    manager = await day.manager_ref(session, bitrix_user_id)
    norm = settings.companion_hygiene_norm_pct
    try:
        async with BitrixClient() as bx:
            # The deal pull and the open-task-owner pull feed three criteria each
            # (statuses/anketa/tasks_set); run them concurrently with the two
            # period-scoped activity reads. The Bitrix client self-throttles, so
            # the fan-out is safe and roughly halves cold wall-time.
            deals_res, owners_res, ontime, notes = await asyncio.gather(
                _open_deals(bx, bitrix_user_id),
                _open_task_deal_ids(bx, bitrix_user_id),
                _tasks_on_time(bx, bitrix_user_id, start, end),
                _notes(bx, bitrix_user_id, start, end),
                return_exceptions=True,
            )
    except BitrixError as exc:
        logger.warning(
            "Hygiene Bitrix read failed for {uid}: {e}", uid=bitrix_user_id, e=exc
        )
        return HygieneView(manager=manager, period=label, norm_pct=norm)

    deals = deals_res if isinstance(deals_res, list) else None
    owners = owners_res if isinstance(owners_res, set) else None
    ontime_crit = (
        ontime
        if isinstance(ontime, HygieneCriterion)
        else _unavailable("tasks_on_time", "Bitrix недоступен")
    )
    notes_crit = (
        notes
        if isinstance(notes, HygieneCriterion)
        else _unavailable("notes", "Bitrix недоступен")
    )

    criteria = [
        _statuses(deals),
        _anketa(deals),
        _tasks_set(deals, owners),
        ontime_crit,
        notes_crit,
    ]
    live = [c.pct for c in criteria if c.status == "live" and c.pct is not None]
    overall = round(sum(live) / len(live), 1) if live else None

    view = HygieneView(
        manager=manager,
        period=label,
        norm_pct=norm,
        overall_pct=overall,
        criteria=criteria,
    )
    expiry = time.monotonic() + settings.companion_hygiene_cache_ttl_seconds
    _cache[cache_key] = (expiry, view)
    return view
