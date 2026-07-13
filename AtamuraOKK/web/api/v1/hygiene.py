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
* **tasks_set**    — «Постановка дел». Of the open deals whose current stage
  *requires* a task per the регламент (``_TASK_STAGES``), the share carrying at
  least one open (incomplete) activity. No-task stages (Новая заявка, Недозвон, …)
  are excluded from the base, so a card parked where no task is due never counts
  against it.
* **tasks_on_time**— «Исполнение дел в сроки». Of activities whose deadline has
  already passed in the period, the share that are not left hanging overdue.
* **notes**        — «Примечание по шаблону». Of the **deals** the manager called in
  the period, the share carrying a note *they* wrote on the card (a
  ``crm.timeline.comment``, matching ``companion_note_template_marker`` when set).
  Deliberately per-deal, not per-call: three Недозвон attempts on one lead are one
  card owing one note. It is *not* read from the call activity's ``DESCRIPTION`` —
  Bitrix telephony leaves that field empty on every call, so the field can only
  ever score 0.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.bitrix.client import PAGE_SIZE
from AtamuraOKK.settings import settings
from AtamuraOKK.web.api.v1 import day, okk
from AtamuraOKK.web.api.v1.schemas import HygieneCriterion, HygieneView

# Bitrix CRM owner type id for a deal (crm.activity OWNER_TYPE_ID).
_DEAL_OWNER_TYPE = 2

# Timeline comments are BB-code. A comment that is only an image (the WhatsApp
# integration posts [img]…[/img]) or only a link (a manager pasting a card URL)
# carries no note, so those blocks go with their contents before the remaining tags
# are stripped — otherwise the bare URL would read as text and score as a note.
_BB_MEDIA = re.compile(r"\[(img|url)[^\]]*\].*?\[/\1\]", re.IGNORECASE | re.DOTALL)
_BB_TAG = re.compile(r"\[/?[^\]]{1,80}\]")
_BARE_URL = re.compile(r"https?://\S+")

# Comment batches in flight at once. A busy month is ~15 batches; the client has no
# pacer of its own (only throttle-retry), so keep the fan-out polite.
_NOTES_BATCH_CONCURRENCY = 4


class _StageTaskRule(NamedTuple):
    """Per-stage task регламент: is a task required, and its deadline window."""

    task_required: bool
    min_deadline: timedelta | None
    max_deadline: timedelta | None


# Zvandau (cat 24) per-stage task регламент. For each open-deal stage: does the
# регламент require the manager to hold a follow-up task, and the [min, max]
# window the task's deadline should fall in after the deal enters the stage
# (measured from stage entry). STATUS_IDs are the stable portal stage ids (mirror
# of day._STAGE_SIGNALS). Only ``task_required`` is consumed today — it scopes the
# tasks_set denominator; the min/max bounds are recorded for the planned per-stage
# window criterion (which needs a per-deal crm.stagehistory pull) and are unused
# for now. Stages absent here — or present with task_required=False — expect no
# task and are excluded from the tasks_set base, so a deal parked in «Новая
# заявка» or «Недозвон» never counts against «Постановка дел».
_TASK_STAGES: dict[str, _StageTaskRule] = {
    "C24:NEW": _StageTaskRule(False, None, None),  # Новая заявка — нет задач
    "C24:PREPARATION": _StageTaskRule(False, None, None),  # Взято в работу — нет задач
    "C24:UC_OPEENZ": _StageTaskRule(  # Попросил перезвонить
        True, timedelta(minutes=10), timedelta(hours=24)
    ),
    "C24:UC_VL3EHH": _StageTaskRule(  # Недозвон 1 — авто-перенос, не ручная задача
        False, timedelta(hours=24), timedelta(hours=48)
    ),
    "C24:UC_LS7DKY": _StageTaskRule(  # Недозвон 2 — нет задач
        False, timedelta(hours=24), timedelta(hours=48)
    ),
    "C24:PREPAYMENT_INVOIC": _StageTaskRule(  # Лид квалифицирован
        True, timedelta(hours=24), timedelta(hours=48)
    ),
    "C24:EXECUTING": _StageTaskRule(  # Записан на встречу ОП
        True, timedelta(hours=12), timedelta(hours=48)
    ),
    "C24:FINAL_INVOICE": _StageTaskRule(  # Подтверждён визит
        True, timedelta(hours=1), timedelta(hours=12)
    ),
    "C24:UC_9OBT14": _StageTaskRule(  # Не дошёл до встречи
        True, timedelta(hours=12), timedelta(hours=48)
    ),
}


def _requires_task(stage_id: str) -> bool:
    """Whether the регламент demands a follow-up task at the deal's current stage."""
    rule = _TASK_STAGES.get(stage_id)
    return rule.task_required if rule else False


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


def _in_period(created: str, start: datetime, end: datetime) -> bool:
    """Whether a Bitrix timestamp falls in the view's period."""
    try:
        moment = datetime.fromisoformat(created)
    except ValueError:
        return False
    return start <= moment < end


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
    """Share of task-requiring open deals that carry an open activity planned.

    Only deals whose current stage requires a task per the регламент
    (``_TASK_STAGES``) enter the base; stages marked «нет задач» (Новая заявка,
    Взято в работу, Недозвон 1/2) are excluded, so a card parked where no task is
    due neither helps nor hurts «Постановка дел».
    """
    if deals is None or deal_ids_with_task is None:
        return _unavailable("tasks_set", "Bitrix недоступен")
    required = [d for d in deals if _requires_task(str(d.get("STAGE_ID") or ""))]
    if not required:
        return _unavailable(
            "tasks_set", "Нет открытых сделок на этапах, где нужна задача"
        )
    with_task = sum(1 for d in required if int(d["ID"]) in deal_ids_with_task)
    note = (
        "База — только сделки на этапах, где регламент требует задачу; этапы "
        "«нет задач» (Новая заявка, Взято в работу, Недозвон 1/2) исключены."
    )
    return _scored("tasks_set", with_task, len(required), note)


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


async def _called_deals(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
) -> tuple[list[int], bool]:
    """Deals the manager called in the period, most recently called first.

    The unit is the **deal card, not the call**: a lead worked through three
    Недозвон attempts is one card owing one note, so calls are collapsed to their
    ``OWNER_ID``. Returns the deal ids (capped at ``companion_hygiene_notes_max_deals``)
    and whether that cap truncated them.
    """
    deal_ids: list[int] = []
    seen: set[int] = set()
    cap = settings.companion_hygiene_notes_max_deals
    async for row in bx.list(
        "crm.activity.list",
        {
            "filter": {
                "RESPONSIBLE_ID": uid,
                "COMPLETED": "Y",
                "TYPE_ID": settings.companion_call_activity_type_id,
                "OWNER_TYPE_ID": _DEAL_OWNER_TYPE,
                ">=CREATED": start.isoformat(),
                "<CREATED": end.isoformat(),
            },
            "select": ["ID", "OWNER_ID"],
            "order": {"ID": "DESC"},
        },
        max_items=settings.companion_hygiene_notes_max_calls,
    ):
        deal_id = int(row.get("OWNER_ID") or 0)
        if not deal_id or deal_id in seen:
            continue
        seen.add(deal_id)
        deal_ids.append(deal_id)
        if len(deal_ids) >= cap:
            return deal_ids, True
    return deal_ids, False


def _is_note(text: str, marker: str) -> bool:
    """Whether a timeline comment counts as a proper post-call note.

    Bitrix stores comments as BB-code, and the Wazzup/WhatsApp integration posts
    image-only comments (``[img]…[/img]``) — those are machine traffic, not a note
    the manager wrote, so strip markup (and media/link blocks whole) before judging
    emptiness.
    """
    text = _BB_MEDIA.sub("", text)
    text = _BB_TAG.sub("", text)
    stripped = _BARE_URL.sub("", text).replace("&nbsp;", " ").strip()
    if len(stripped) < settings.companion_note_min_chars:
        return False
    return bool(stripped) and (not marker or marker in stripped.lower())


async def _notes(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
) -> HygieneCriterion:
    """Share of the deals the manager called that carry a note they wrote.

    The note lives on the **deal timeline**, not on the call activity: Bitrix
    telephony writes the call activity itself, and its ``DESCRIPTION`` is always
    empty (nothing in the UI ever fills it), so the old per-call reading of that
    field could only ever return 0. A manager's note is a ``crm.timeline.comment``
    on the deal card — the one the ОКК регламент means by «примечание по шаблону».
    """
    marker = settings.companion_note_template_marker.strip().lower()
    gate = asyncio.Semaphore(_NOTES_BATCH_CONCURRENCY)

    async def noted_in(deal_ids: list[int]) -> int:
        """How many of these (≤ PAGE_SIZE) deals carry a note by the manager."""
        async with gate:
            comments = await bx.batch(
                {
                    str(deal_id): (
                        "crm.timeline.comment.list",
                        {
                            "filter[ENTITY_ID]": deal_id,
                            "filter[ENTITY_TYPE]": "deal",
                            "select[]": ["ID", "COMMENT", "AUTHOR_ID", "CREATED"],
                        },
                    )
                    for deal_id in deal_ids
                },
            )
        return sum(
            any(
                int(r.get("AUTHOR_ID") or 0) == uid
                and _in_period(str(r.get("CREATED") or ""), start, end)
                and _is_note(str(r.get("COMMENT") or ""), marker)
                for r in comments.get(str(deal_id)) or []
            )
            for deal_id in deal_ids
        )

    try:
        deal_ids, truncated = await _called_deals(bx, uid, start, end)
        if not deal_ids:
            return _unavailable("notes", "Нет звонков по сделкам за период")
        # Comments are per-entity in Bitrix (no cross-deal filter), so a month of
        # calls means hundreds of reads: pack them PAGE_SIZE to a batch and run a
        # few batches at a time (the client retries throttling, it doesn't pace).
        chunks = [
            deal_ids[i : i + PAGE_SIZE] for i in range(0, len(deal_ids), PAGE_SIZE)
        ]
        with_note = sum(await asyncio.gather(*(noted_in(c) for c in chunks)))
    except BitrixError:
        return _unavailable("notes", "Bitrix недоступен")

    base = (
        f"База — последние {len(deal_ids)} сделок, по которым были звонки "
        "(лимит периода)."
        if truncated
        else f"База — {len(deal_ids)} сделок, по которым были звонки в периоде."
    )
    rule = (
        f" Засчитывается примечание менеджера в карточке с меткой «{marker}»."
        if marker
        else " Засчитывается любое содержательное примечание менеджера в карточке "
        "сделки (комментарий в таймлайне); автосообщения интеграций не в счёт."
    )
    return _scored("notes", with_note, len(deal_ids), base + rule)


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
