"""Live «ОКК · Гигиена CRM» read-through over the Bitrix TM funnel.

Like :mod:`day` and :mod:`analytics`, this reads **straight through to Bitrix**
per request (short TTL cache) rather than OKK's Postgres — it measures the
discipline of keeping the deal card in order *after* the call, which lives in the
CRM, not in the scoring tables.

**Every criterion is scoped to the requested period**, so неделя and месяц give
genuinely different numbers. The two activity criteria window their own Bitrix
reads by ``DEADLINE``/``CREATED``; the three card criteria share one deal pull
scoped by ``DATE_CREATE`` (``_period_deals``) — the base is «карточки, заведённые
в периоде», closed ones included, so a past week keeps reporting the same cards
instead of emptying out as they close.

Five criteria, each independently resilient (a failing sub-read degrades that one
criterion to ``status="not_available"`` rather than failing the whole view):

* **statuses**     — «Правильное вписывание статусов». Proxy: share of the period's
  TM deals that were NOT stale as of the period's end (activity within
  ``companion_hygiene_stale_days`` of ``min(end, now)``); a closed card is never
  stale. The strict "stage matches the call outcome" check needs an ОКК
  transcript↔stage comparison on the scoring side (not wired); a stuck, untouched
  card is the computable signal that the status is not kept.
* **anketa**       — «Правильное заполнение анкеты». Of the period's deals that
  reached a stage where the questionnaire is owed (``_ANKETA_STAGES``, plus any
  won deal), the share with every configured field (``companion_anketa_fields``)
  filled. Leads still in dialling owe no анкета and stay out of the base.
  Unconfigured → not_available (we never invent a field list).
* **tasks_set**    — «Постановка дел». Of the period's still-open deals whose
  current stage *requires* a task per the регламент (``_TASK_STAGES``), the share
  carrying at least one open (incomplete) activity. No-task stages (Новая заявка,
  Недозвон, …) and closed cards are excluded from the base, so a card parked where
  no task is due never counts against it. The base is period-scoped but the
  has-a-task check is a *now* reading — per-stage history would need a per-deal
  crm.stagehistory pull.
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

from AtamuraOKK.bitrix import BitrixClient, BitrixError, crm_card_url
from AtamuraOKK.bitrix.client import PAGE_SIZE
from AtamuraOKK.settings import settings
from AtamuraOKK.web.api.v1 import day, okk
from AtamuraOKK.web.api.v1.schemas import (
    HygieneCriterion,
    HygieneFailedItem,
    HygieneView,
)

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


# Stages at which the questionnaire is already owed. The анкета is what the manager
# fills *while qualifying* the lead, so a card still sitting in «Новая заявка»,
# «Взято в работу», «Недозвон» or «Попросил перезвонить» owes nothing yet — Bitrix
# measures 0–8% filled there against 100% from «Лид квалифицирован» on. Counting
# those stages in the base would cap even a perfect manager near 20%, so they are
# excluded, exactly as «нет задач» stages are excluded from tasks_set.
_ANKETA_STAGES = frozenset(
    {
        "C24:PREPAYMENT_INVOIC",  # Лид квалифицирован
        "C24:EXECUTING",  # Записан на встречу в ОП
        "C24:FINAL_INVOICE",  # Подтверждён визит
        "C24:UC_9OBT14",  # Не дошёл до встречи
    }
)


def _anketa_due(stage_id: str) -> bool:
    """Whether the deal has reached a stage at which the анкета must be filled."""
    return stage_id in _ANKETA_STAGES


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


def _deal_item(d: dict[str, Any], detail: str | None) -> HygieneFailedItem:
    """A failing-card entry for an open deal row (title/stage/deep-link)."""
    deal_id = int(d["ID"])
    return HygieneFailedItem(
        entity_id=deal_id,
        title=str(d.get("TITLE") or "").strip() or None,
        stage=day.stage_label(str(d.get("STAGE_ID") or "")),
        detail=detail,
        bitrix_url=crm_card_url("DEAL", deal_id),
    )


def _cap(items: list[HygieneFailedItem]) -> tuple[list[HygieneFailedItem], bool]:
    """Trim the failing list to the drill-down cap; flag whether it truncated."""
    limit = settings.companion_hygiene_failed_max_items
    return items[:limit], len(items) > limit


def _unavailable(key: str, note: str) -> HygieneCriterion:
    return HygieneCriterion(key=key, status="not_available", note=note)


def _scored(
    key: str,
    numerator: int,
    denominator: int,
    note: str | None = None,
    failed: list[HygieneFailedItem] | None = None,
    failed_truncated: bool = False,
) -> HygieneCriterion:
    return HygieneCriterion(
        key=key,
        status="live",
        pct=_pct(numerator, denominator),
        numerator=numerator,
        denominator=denominator,
        note=note,
        failed_items=failed or [],
        failed_truncated=failed_truncated,
    )


async def _period_deals(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """TM-funnel deals the manager *opened in the period*, anketa fields selected.

    Scoped by ``DATE_CREATE``, not by ``CLOSED``: the base is «карточки, заведённые
    на той неделе», so a week and a month give genuinely different (strictly nested)
    numbers, and a past period keeps returning the same cards as they close instead
    of quietly emptying out. Each criterion decides for itself what a closed card
    means — ``CLOSED``/``STAGE_SEMANTIC_ID`` come along for that.
    """
    select = [
        "ID",
        "TITLE",
        "STAGE_ID",
        "STAGE_SEMANTIC_ID",
        "CLOSED",
        "DATE_CREATE",
        "LAST_ACTIVITY_TIME",
        *settings.companion_anketa_fields,
    ]
    return [
        d
        async for d in bx.list(
            "crm.deal.list",
            {
                "filter": {
                    "CATEGORY_ID": settings.companion_tm_category_id,
                    "ASSIGNED_BY_ID": uid,
                    ">=DATE_CREATE": start.isoformat(),
                    "<DATE_CREATE": end.isoformat(),
                },
                "select": select,
                "order": {"DATE_CREATE": "ASC"},
            },
            max_items=settings.companion_hygiene_deals_max_scan,
        )
    ]


def _truncated_deals(deals: list[dict[str, Any]] | None) -> bool:
    """Whether the period deal pull hit its cap (so the base is a partial sample)."""
    return deals is not None and len(deals) >= settings.companion_hygiene_deals_max_scan


_TRUNCATED_NOTE = (
    " Внимание: сделок в периоде больше лимита выборки — показана только часть, "
    "проценты приблизительные."
)


def _is_closed(d: dict[str, Any]) -> bool:
    return str(d.get("CLOSED") or "").upper() == "Y"


def _is_won(d: dict[str, Any]) -> bool:
    """Deal closed successfully (Bitrix stage semantics: S = success)."""
    return str(d.get("STAGE_SEMANTIC_ID") or "").upper() == "S"


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


def _statuses(deals: list[dict[str, Any]] | None, end: datetime) -> HygieneCriterion:
    """Share of the period's deals not left hanging as of the period's end.

    Staleness is measured against ``min(end, now)``, not against today: for a past
    week the question is «была ли карточка заброшена на тот момент», and anchoring
    to the period end keeps that answer stable instead of drifting staler with every
    day that passes. A closed card counts as maintained — a deal that was carried to
    won/lost is by definition not left hanging.
    """
    if deals is None:
        return _unavailable("statuses", "Bitrix недоступен")
    if not deals:
        return _unavailable("statuses", "Нет сделок, заведённых в периоде")
    as_of = min(end, _now())
    cutoff = as_of - timedelta(days=settings.companion_hygiene_stale_days)
    maintained = 0
    failed: list[HygieneFailedItem] = []
    for d in deals:
        if _is_closed(d):
            maintained += 1
            continue
        raw = d.get("LAST_ACTIVITY_TIME")
        last: datetime | None = None
        if raw:
            try:
                last = datetime.fromisoformat(str(raw))
            except ValueError:
                last = None
        if last is not None and last >= cutoff:
            maintained += 1
        else:
            detail = (
                f"нет активности {(as_of - last).days} дн."
                if last is not None
                else "ни одной активности"
            )
            failed.append(_deal_item(d, detail))
    note = (
        f"База — сделки, заведённые в периоде. Прокси-метрика: открытая сделка без "
        f"активности дольше {settings.companion_hygiene_stale_days} дн. (на конец "
        "периода) считается зависшей; закрытая карточка зависшей не считается. "
        "Строгая сверка статуса с исходом звонка — на стороне ОКК-скоринга (в плане)."
    )
    items, truncated = _cap(failed)
    return _scored("statuses", maintained, len(deals), note, items, truncated)


def _anketa(deals: list[dict[str, Any]] | None) -> HygieneCriterion:
    """Share of qualified period deals with every configured questionnaire field filled.

    Only deals that reached a stage where the анкета is owed (``_ANKETA_STAGES``)
    enter the base; a lead still being dialled has no questionnaire due and so
    neither helps nor hurts the criterion. A won deal is in the base whatever stage
    id it now carries — it demonstrably passed qualification. A lost deal is not:
    its current stage no longer says how far it actually got, and reconstructing
    that needs a per-deal crm.stagehistory pull.
    """
    fields = settings.companion_anketa_fields
    if not fields:
        return _unavailable(
            "anketa",
            "Не задан список полей анкеты (ATAMURAOKK_COMPANION_ANKETA_FIELDS)",
        )
    if deals is None:
        return _unavailable("anketa", "Bitrix недоступен")
    due = [d for d in deals if _is_won(d) or _anketa_due(str(d.get("STAGE_ID") or ""))]
    if not due:
        return _unavailable("anketa", "Нет сделок на этапах, где анкета уже нужна")
    complete = 0
    failed: list[HygieneFailedItem] = []
    for d in due:
        missing = sum(1 for f in fields if not _filled(d.get(f)))
        if missing:
            failed.append(
                _deal_item(d, f"не заполнено {missing} из {len(fields)} полей анкеты")
            )
        else:
            complete += 1
    note = (
        "База — сделки периода, дошедшие до квалификации («Лид квалифицирован» и "
        "далее, включая успешно закрытые); лиды в дозвоне анкеты ещё не должны. "
        "Засчитывается карточка, где заполнены все поля анкеты."
    )
    items, truncated = _cap(failed)
    return _scored("anketa", complete, len(due), note, items, truncated)


def _tasks_set(
    deals: list[dict[str, Any]] | None,
    deal_ids_with_task: set[int] | None,
) -> HygieneCriterion:
    """Share of task-requiring period deals that carry an open activity planned.

    Only deals whose current stage requires a task per the регламент
    (``_TASK_STAGES``) enter the base; stages marked «нет задач» (Новая заявка,
    Взято в работу, Недозвон 1/2) are excluded, so a card parked where no task is
    due neither helps nor hurts «Постановка дел». Closed cards owe no follow-up and
    drop out too.
    """
    if deals is None or deal_ids_with_task is None:
        return _unavailable("tasks_set", "Bitrix недоступен")
    required = [
        d
        for d in deals
        if not _is_closed(d) and _requires_task(str(d.get("STAGE_ID") or ""))
    ]
    if not required:
        return _unavailable(
            "tasks_set", "Нет открытых сделок периода на этапах, где нужна задача"
        )
    with_task = 0
    failed: list[HygieneFailedItem] = []
    for d in required:
        if int(d["ID"]) in deal_ids_with_task:
            with_task += 1
        else:
            failed.append(_deal_item(d, "нет запланированного дела"))
    note = (
        "База — сделки периода на этапах, где регламент требует задачу; этапы "
        "«нет задач» (Новая заявка, Взято в работу, Недозвон 1/2) и закрытые "
        "карточки исключены. Наличие дела проверяется на текущий момент."
    )
    items, truncated = _cap(failed)
    return _scored("tasks_set", with_task, len(required), note, items, truncated)


async def _overdue_items(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    due_end: datetime,
) -> list[HygieneFailedItem]:
    """The still-open, past-deadline activities in the window (capped), for the list.

    Each links to the CRM entity it hangs off of (usually the deal). The count API
    gives the exact numerator/denominator; this is only the drill-down sample.
    """
    limit = settings.companion_hygiene_failed_max_items
    items: list[HygieneFailedItem] = []
    async for row in bx.list(
        "crm.activity.list",
        {
            "filter": {
                "RESPONSIBLE_ID": uid,
                "COMPLETED": "N",
                ">=DEADLINE": start.isoformat(),
                "<DEADLINE": due_end.isoformat(),
            },
            "select": ["ID", "SUBJECT", "OWNER_ID", "OWNER_TYPE_ID", "DEADLINE"],
            "order": {"DEADLINE": "ASC"},
        },
        max_items=limit,
    ):
        owner_id = int(row.get("OWNER_ID") or 0)
        entity = day.activity_owner_entity(int(row.get("OWNER_TYPE_ID") or 0))
        deadline = str(row.get("DEADLINE") or "")[:10]
        items.append(
            HygieneFailedItem(
                entity_id=owner_id,
                title=str(row.get("SUBJECT") or "").strip() or f"Дело #{row.get('ID')}",
                detail=f"просрочено (срок {deadline})" if deadline else "просрочено",
                bitrix_url=crm_card_url(entity, owner_id) if owner_id else None,
            )
        )
    return items


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
        failed = await _overdue_items(bx, uid, start, due_end) if overdue else []
    except BitrixError:
        return _unavailable("tasks_on_time", "Bitrix недоступен")
    if not due:
        return _unavailable("tasks_on_time", "Нет дел с наступившим сроком")
    note = (
        "Дело с наступившим сроком, оставшееся незакрытым, считается просроченным. "
        "Строгое «закрыто точно в срок» требует таймстемпа закрытия активности "
        "(его нет в count-API), поэтому закрытое с опозданием тут засчитывается."
    )
    return _scored(
        "tasks_on_time", due - overdue, due, note, failed, overdue > len(failed)
    )


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


async def _deal_titles(bx: BitrixClient, deal_ids: list[int]) -> dict[int, str]:
    """Titles for a bounded set of deal ids (one page), for the failing-notes list."""
    if not deal_ids:
        return {}
    out: dict[int, str] = {}
    async for d in bx.list(
        "crm.deal.list",
        {"filter": {"ID": deal_ids}, "select": ["ID", "TITLE"]},
        max_items=len(deal_ids),
    ):
        title = str(d.get("TITLE") or "").strip()
        if title:
            out[int(d["ID"])] = title
    return out


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

    async def noted_in(deal_ids: list[int]) -> set[int]:
        """Which of these (≤ PAGE_SIZE) deals carry a note by the manager."""
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
        return {
            deal_id
            for deal_id in deal_ids
            if any(
                int(r.get("AUTHOR_ID") or 0) == uid
                and _in_period(str(r.get("CREATED") or ""), start, end)
                and _is_note(str(r.get("COMMENT") or ""), marker)
                for r in comments.get(str(deal_id)) or []
            )
        }

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
        noted: set[int] = set()
        for chunk_noted in await asyncio.gather(*(noted_in(c) for c in chunks)):
            noted |= chunk_noted
        # deal_ids is most-recently-called first, so the failing sample surfaces the
        # freshest un-noted cards; titles are one more small read for just the cap.
        missing = [d for d in deal_ids if d not in noted]
        capped = missing[: settings.companion_hygiene_failed_max_items]
        titles = await _deal_titles(bx, capped)
        failed = [
            HygieneFailedItem(
                entity_id=deal_id,
                title=titles.get(deal_id),
                detail="нет примечания в карточке",
                bitrix_url=crm_card_url("DEAL", deal_id),
            )
            for deal_id in capped
        ]
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
    return _scored(
        "notes",
        len(noted),
        len(deal_ids),
        base + rule,
        failed,
        len(missing) > len(capped),
    )


async def get_hygiene(
    session: AsyncSession,
    bitrix_user_id: int,
    period: str | None,
    refresh: bool = False,
) -> HygieneView:
    """Live CRM-hygiene view for a manager (Bitrix user id) in a period.

    ``refresh`` bypasses the short TTL cache so a manager who just fixed a flagged
    card sees the index move on the very next load (the «Проверить снова» button).
    """
    start, end, label = okk.parse_period(period)
    cache_key = (bitrix_user_id, label)
    if not refresh:
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
                _period_deals(bx, bitrix_user_id, start, end),
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

    card_criteria = [_statuses(deals, end), _anketa(deals), _tasks_set(deals, owners)]
    if _truncated_deals(deals):
        # Silent truncation would read as «мы посчитали всё» — say it out loud.
        logger.warning(
            "Hygiene deal pull hit the scan cap for {uid} in {label}",
            uid=bitrix_user_id,
            label=label,
        )
        for crit in card_criteria:
            crit.note = (crit.note or "") + _TRUNCATED_NOTE

    criteria = [*card_criteria, ontime_crit, notes_crit]
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
