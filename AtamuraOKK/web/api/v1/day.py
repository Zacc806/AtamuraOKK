"""Live "Мой день" read-through over the Bitrix cat-0 (Телемаркетинг) funnel.

Unlike the rest of ``/api/v1`` (which reads OKK's Postgres), the day view reads
**straight through to Bitrix** per request (short TTL cache): it is an inherently
real-time screen ("кому звонить сейчас", "встречи сегодня") and the data lives in
the TM's own deal pipeline, owned by them via ``ASSIGNED_BY_ID``. OKK still owns
the Bitrix gateway, so the companion stays a thin consumer.

Meeting attribution (see docs/companion-day.md): a deal never *rests* at the
meeting stage — at the moment of the visit it is moved to cat 2 and reassigned
to the sales closer, so a live stage+assignee filter always counts 0. The
conducted-meeting fact survives in ``crm.stagehistory.list`` and the TM survives
in the «Сотрудник ТМ» employee field; ``_meetings_by_tm`` joins the two. When a
manager has no live pipeline the view returns ``data_ready=False`` so the UI
shows "данные готовятся", never fake numbers.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.bitrix import BitrixClient, BitrixError, crm_card_url
from AtamuraOKK.db.models.audit_verdict import AuditVerdict
from AtamuraOKK.db.models.department import Department
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.settings import settings
from AtamuraOKK.web.api.v1 import okk
from AtamuraOKK.web.api.v1.schemas import (
    AuditFailedItem,
    DayActionItem,
    DayStats,
    DayTaskItem,
    DayToday,
    DayView,
    ManagerRef,
    MoneyAxis,
    NoMeetingItem,
    OverdueTaskItem,
)

# Zvandau (cat 24) deal-stage STATUS_ID -> (reason, heat, stat-bucket). Stage
# names are the operator's own funnel labels (discovered via crm.status.list);
# STATUS_IDs are stable per portal. ``bucket`` feeds the three Мой день counters.
_STAGE_SIGNALS: dict[str, tuple[str, str, str | None]] = {
    "C24:NEW": ("Новая заявка — обработать", "warm", None),
    "C24:PREPARATION": ("Взято в работу — двигать к встрече", "warm", None),
    "C24:UC_OPEENZ": ("Просил перезвонить", "hot", None),
    "C24:UC_VL3EHH": ("Недозвон 1 — перезвонить", "warm", "no_answer"),
    "C24:UC_LS7DKY": ("Недозвон 2 — последняя попытка", "warm", "no_answer"),
    "C24:PREPAYMENT_INVOIC": ("Квалифицирован — записать на встречу", "hot", None),
    "C24:EXECUTING": ("Записан на встречу — подтвердить за день", "cool", "meetings"),
    "C24:FINAL_INVOICE": ("Визит подтверждён", "cool", "meetings"),
    "C24:UC_9OBT14": ("Не дошёл до встречи — перезаписать", "hot", "cooling"),
    "C24:UC_8PKXOA": ("Дубль — проверить и закрыть", "cool", None),
    "C24:UC_5UCLAR": ("Встреча без ТМ — уточнить", "cool", None),
}
_DEFAULT_SIGNAL = ("В работе — следующий шаг к встрече", "warm", None)
_STALE_DAYS = 7  # an open deal idle this long counts as "остывает"
# crm.activity.list OWNER_TYPE_ID -> CRM entity, for linking an overdue task
# to the deal/contact/lead it hangs off of.
_ACTIVITY_OWNER_ENTITY = {1: "LEAD", 2: "DEAL", 3: "CONTACT", 4: "COMPANY"}
_DEAL_OWNER_TYPE_ID = 2  # crm.activity.list OWNER_TYPE_ID for a deal
# Deadlines at/below this are Bitrix's empty/zero date ("0000-00-00"): a
# deadline-less activity, not an overdue task. The team overdue query floors on
# it so no-deadline activities don't masquerade as maximally overdue.
_DEADLINE_FLOOR = "2000-01-01T00:00:00"
# Hot pre-booking stages — a deal entering one of these is ripe to push to a
# meeting ("дожать до встречи"). Derived from the signal map so it stays in sync.
_HOT_STAGES = [stage for stage, sig in _STAGE_SIGNALS.items() if sig[1] == "hot"]

# Closing-block («6. Резюме + Закрытие на КЭВ») criterion ids, per rubric version.
# The numeric ids are version-specific; only the block_id ("closing") is stable.
# ``booked`` is the element that asserts a concrete date+time was fixed — its НЕТ
# is what puts a call in the callback queue; the others merely explain the miss.
# A rubric absent from this map yields no rows rather than a guessed verdict:
# tm-call-v2 lumped the whole close into one weighted element, so it cannot say
# whether a meeting was actually booked.
_CLOSING_CRITERIA: dict[str, dict[str, int]] = {
    "tm-call-v4": {
        "booked": 26,  # Зафиксировал дату + время записи в ОП
        "time": 24,  # Предложил конкретное время с выбором
        "retry": 25,  # При отказе — повторная попытка закрыть
        "value": 23,  # Презентовал ценность встречи
    },
}

# (uid, period_label, day_label) -> (monotonic expiry, DayView). Tiny in-process
# TTL cache so rapid re-opens / tab switches don't hammer Bitrix. ``day_label`` is
# the «Важные цифры дня» window ("today" or a YYYY-MM-DD past day).
_cache: dict[tuple[int, str, str], tuple[float, DayView]] = {}

# (distinct deals entering a stage per assignee, deals re-entering it 2+ times per
# assignee) — what stage_outcomes_by_assignee returns.
_StageOutcomes = tuple[dict[int, int], dict[int, int]]


def stage_label(stage_id: str) -> str | None:
    """Human funnel label for a Zvandau (cat 24) stage id, if known."""
    sig = _STAGE_SIGNALS.get(stage_id)
    return sig[0] if sig else None


def activity_owner_entity(owner_type_id: int) -> str | None:
    """CRM entity name (DEAL/CONTACT/LEAD/COMPANY) for an activity OWNER_TYPE_ID."""
    return _ACTIVITY_OWNER_ENTITY.get(owner_type_id)


def _phone_of(contact: dict[str, Any]) -> str | None:
    phones = contact.get("PHONE") or []
    for p in phones:
        if p.get("VALUE"):
            return str(p["VALUE"])
    return None


def _name_of(contact: dict[str, Any]) -> str | None:
    parts = [contact.get(k) for k in ("NAME", "LAST_NAME") if contact.get(k)]
    return " ".join(str(p) for p in parts).strip() or None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


async def _manager_ref(session: AsyncSession, bitrix_user_id: int) -> ManagerRef:
    """Identity from OKK's DB if known, else a bare ref (Bitrix is the source)."""
    manager = await session.scalar(
        select(Manager).where(Manager.bitrix_user_id == bitrix_user_id),
    )
    if manager is None:
        return ManagerRef(bitrix_user_id=bitrix_user_id)
    department = (
        await session.get(Department, manager.department_id)
        if manager.department_id
        else None
    )
    name = " ".join(p for p in (manager.name, manager.last_name) if p) or None
    return ManagerRef(
        bitrix_user_id=bitrix_user_id,
        name=name,
        department_id=department.bitrix_id if department else None,
        department_name=department.name if department else None,
    )


async def _audit_failed_items(
    session: AsyncSession, bitrix_user_id: int, limit: int
) -> list[AuditFailedItem]:
    """Closed-lost deals whose stated reason contradicted the call, for this manager.

    A Postgres read of OKK's ``audit_verdicts`` (verdict = ``contradicted``), scoped
    to the manager by ``managers.bitrix_user_id``. Independent of the live Bitrix
    read, so the «Отказы не по делу» queue survives a Bitrix outage.
    """
    rows = (
        (
            await session.execute(
                select(AuditVerdict)
                .join(Manager, Manager.id == AuditVerdict.manager_id)
                .where(
                    Manager.bitrix_user_id == bitrix_user_id,
                    AuditVerdict.verdict == "contradicted",
                )
                .order_by(AuditVerdict.audited_at.desc())
                .limit(limit),
            )
        )
        .scalars()
        .all()
    )
    return [
        AuditFailedItem(
            deal_id=r.bitrix_deal_id,
            client_name=r.deal_title,
            close_reason=r.close_reason,
            justification=r.justification,
            evidence_quote=r.evidence_quote,
            confidence=r.confidence,
            audited_at=r.audited_at,
            bitrix_url=crm_card_url("DEAL", r.bitrix_deal_id),
        )
        for r in rows
    ]


def _closing_scores(criteria: dict[str, Any]) -> dict[int, int]:
    """``criterion_id -> ДА(1)/НЕТ(0)`` for the closing block of one scored call.

    Н.П. elements are absent from ``per_criterion`` altogether (the scorer drops
    them so they leave the denominator), so a missing id means "not applicable",
    never "failed" — callers must not read absence as a НЕТ.
    """
    out: dict[int, int] = {}
    for pc in criteria.get("per_criterion") or []:
        if pc.get("block_id") != "closing":
            continue
        try:
            out[int(pc["id"])] = int(pc.get("score") or 0)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _no_meeting_reason(scores: dict[int, int], ids: dict[str, int]) -> str:
    """Why the close failed, in the rubric's own terms — the manager's next move.

    Ordered by what is most worth fixing on the callback: an unanswered doubt
    beats a missing time slot, which beats a valueless invite.
    """
    if scores.get(ids["retry"]) == 0:
        return "клиент засомневался — дожать не пытались"
    if scores.get(ids["time"]) == 0:
        return "конкретное время встречи не предложили"
    if scores.get(ids["value"]) == 0:
        return "позвал на встречу без ценности"
    return "дату и время записи не зафиксировали"


async def _no_meeting_items(
    session: AsyncSession,
    bitrix_user_id: int,
    start: datetime,
    end: datetime,
    limit: int,
) -> list[NoMeetingItem]:
    """Целевые calls the rubric says never got a meeting booked, for this manager.

    A Postgres read of OKK's own scores: the closing block's «Зафиксировал дату +
    время записи в ОП» came back НЕТ, so nothing reached the calendar and the
    client is still callable. Клиент, который согласился приехать и всё равно не
    записан, идёт первым — он самый тёплый. Independent of the live Bitrix read
    (names are filled in later, best-effort), so both the queue and the number to
    dial survive a Bitrix outage.
    """
    rows = (
        await session.execute(
            text(
                "SELECT cs.call_id, cs.started_at, cs.rubric_version, "
                "cs.crm_entity_type, cs.crm_entity_id, c.phone_number, s.criteria "
                "FROM call_scores_latest cs "
                "JOIN scores s ON s.id = cs.score_id "
                "JOIN calls c ON c.id = cs.call_id "
                "WHERE cs.manager_bitrix_user_id = :uid "
                "AND cs.target_status = 'целевой' "
                "AND cs.started_at >= :start AND cs.started_at < :end "
                "ORDER BY cs.started_at DESC",
            ),
            {"uid": bitrix_user_id, "start": start, "end": end},
        )
    ).all()

    items: list[NoMeetingItem] = []
    for r in rows:
        ids = _CLOSING_CRITERIA.get(r.rubric_version)
        criteria = r.criteria if isinstance(r.criteria, dict) else {}
        if ids is None or not criteria:
            continue
        scores = _closing_scores(criteria)
        if scores.get(ids["booked"]) != 0:
            continue  # booked, or the element was Н.П. — either way, not ours
        wants_to_visit = criteria.get("wants_to_visit")
        is_contact = r.crm_entity_type == "CONTACT" and r.crm_entity_id
        items.append(
            NoMeetingItem(
                call_id=r.call_id,
                started_at=r.started_at,
                contact_id=int(r.crm_entity_id) if is_contact else None,
                phone=r.phone_number,
                wants_to_visit=wants_to_visit,
                reason=(
                    "хотел приехать — так и не записали"
                    if wants_to_visit
                    else _no_meeting_reason(scores, ids)
                ),
                bitrix_url=crm_card_url(r.crm_entity_type, r.crm_entity_id)
                if r.crm_entity_type and r.crm_entity_id
                else None,
            ),
        )

    # Warmest first (client said yes and still wasn't booked), then freshest —
    # a client who talked yesterday is likelier to pick up than one from week one.
    items.sort(
        key=lambda i: (
            bool(i.wants_to_visit),
            i.started_at or datetime.min.replace(tzinfo=UTC),
        ),
        reverse=True,
    )
    return items[:limit]


async def _fill_client_names(
    bx: BitrixClient,
    items: Sequence[NoMeetingItem],
) -> None:
    """Fill in contact names on the callback queue, in place. Best-effort.

    Only CONTACT-linked calls carry a resolvable name; anything else keeps the
    phone number the card already shows. A Bitrix failure here must never cost
    the manager the queue, so the caller treats the whole step as optional.
    """
    by_contact: dict[int, list[NoMeetingItem]] = {}
    for it in items:
        if it.contact_id:
            by_contact.setdefault(it.contact_id, []).append(it)
    if not by_contact:
        return
    contacts = await _contacts(bx, set(by_contact))
    for cid, group in by_contact.items():
        contact = contacts.get(cid)
        if contact is None:
            continue
        name = _name_of(contact)
        for it in group:
            it.client_name = name


async def _open_deals(bx: BitrixClient, uid: int) -> list[dict[str, Any]]:
    """Open TM-funnel deals for the manager, stalest first (most urgent to touch).

    Returns up to ``max_scan`` so the stat counters reflect the whole pipeline;
    the action list later takes only the first ``max_actions`` of these.
    """
    params = {
        "filter": {
            "CATEGORY_ID": settings.companion_tm_category_id,
            "ASSIGNED_BY_ID": uid,
            "CLOSED": "N",
        },
        "select": ["ID", "TITLE", "STAGE_ID", "CONTACT_ID", "LAST_ACTIVITY_TIME"],
        "order": {"LAST_ACTIVITY_TIME": "ASC"},
    }
    return [
        d
        async for d in bx.list(
            "crm.deal.list",
            params,
            max_items=settings.companion_day_max_scan,
        )
    ]


async def _contacts(bx: BitrixClient, ids: set[int]) -> dict[int, dict[str, Any]]:
    """Resolve a batch of contact ids to name/phone in one list call."""
    if not ids:
        return {}
    select = ["ID", "NAME", "LAST_NAME", "PHONE"]
    rows = [
        c
        async for c in bx.list(
            "crm.contact.list",
            {"filter": {"ID": sorted(ids)}, "select": select},
        )
    ]
    return {int(c["ID"]): c for c in rows}


async def _deals_with_open_task(
    bx: BitrixClient,
    deal_ids: set[int],
) -> set[int]:
    """Which of ``deal_ids`` have at least one open (incomplete) activity.

    A deal with no open activity is a «брошенная» card without a next task — the
    complement is the «Без задачи» queue. One ``crm.activity.list`` pass, batched
    50 deals at a time (the OWNER_ID filter takes a list), COMPLETED='N' over deal
    owners. Returns the set of deals that *have* a task, so the caller can flag
    the rest.
    """
    with_task: set[int] = set()
    ids = sorted(deal_ids)
    for i in range(0, len(ids), 50):
        async for r in bx.list(
            "crm.activity.list",
            {
                "filter": {
                    "OWNER_TYPE_ID": _DEAL_OWNER_TYPE_ID,
                    "OWNER_ID": ids[i : i + 50],
                    "COMPLETED": "N",
                },
                "select": ["ID", "OWNER_ID"],
            },
        ):
            owner = r.get("OWNER_ID")
            if owner:
                with_task.add(int(owner))
    return with_task


async def _count_list(
    bx: BitrixClient,
    method: str,
    filter_: dict[str, Any],
) -> int:
    """Total rows for a list method, via the envelope ``total`` (no paging)."""
    env = await bx.call_raw(method, {"filter": filter_, "select": ["ID"]})
    total = env.get("total")
    return int(total) if total is not None else len(env.get("result") or [])


async def _count(bx: BitrixClient, filter_: dict[str, Any]) -> int:
    """Total rows for a deal filter, via the list envelope (no paging)."""
    return await _count_list(bx, "crm.deal.list", filter_)


# period "start/end" -> (monotonic expiry, {tm_user_id: count}). One stage-history
# pull serves every manager in the period (team views hit it once per TTL, not
# once per manager). One cache per (category, stage) the join is run for.
# _meetings_cache: cat-24 «Фактический визит»; _sold_cache: cat-2 «БРОНЬ ПОДПИСАН».
_meetings_cache: dict[str, tuple[float, dict[int, int]]] = {}
_sold_cache: dict[str, tuple[float, dict[int, int]]] = {}


async def _won_by_tm(
    bx: BitrixClient,
    category_id: int,
    stage_id: str,
    cache: dict[str, tuple[float, dict[int, int]]],
    start: datetime,
    end: datetime,
) -> dict[int, int]:
    """Distinct deals entering ``(category_id, stage_id)`` in the period, per TM.

    Attributed through the deal's «Сотрудник ТМ» employee field, not assignee:
    such deals have been reassigned to the closer (and the WON one moved to the
    sales funnel), so a live stage+assignee filter yields 0 — the fact survives
    only in ``crm.stagehistory.list`` + the persisted TM field. Used both for
    «Фактический визит» (cat-24 WON, the conducted meeting) and «БРОНЬ ПОДПИСАН»
    (cat-2 WON, the booking signed = «купили»), with a cache each.
    """
    s, e = start.date().isoformat(), end.date().isoformat()
    cache_key = f"{s}/{e}"
    hit = cache.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    deal_ids: set[int] = set()
    cursor: int | None = 0
    while cursor is not None:
        env = await bx.call_raw(
            "crm.stagehistory.list",
            {
                "entityTypeId": 2,  # deals
                "filter": {
                    "CATEGORY_ID": category_id,
                    "STAGE_ID": stage_id,
                    ">=CREATED_TIME": s,
                    "<CREATED_TIME": e,
                },
                "select": ["OWNER_ID"],
                "start": cursor,
            },
        )
        result = env.get("result") or {}
        items = result.get("items") if isinstance(result, dict) else result
        deal_ids.update(int(it["OWNER_ID"]) for it in items or [])
        nxt = env.get("next")
        cursor = int(nxt) if nxt is not None else None

    field = settings.companion_tm_employee_field
    counts: dict[int, int] = {}
    ids = sorted(deal_ids)
    for i in range(0, len(ids), 50):
        async for row in bx.list(
            "crm.deal.list",
            {"filter": {"ID": ids[i : i + 50]}, "select": ["ID", field]},
        ):
            tm = row.get(field)
            if tm and str(tm) != "0":
                counts[int(tm)] = counts.get(int(tm), 0) + 1

    expiry = time.monotonic() + settings.companion_day_cache_ttl_seconds
    cache[cache_key] = (expiry, counts)
    return counts


async def _meetings_by_tm(
    bx: BitrixClient,
    start: datetime,
    end: datetime,
) -> dict[int, int]:
    """Conducted meetings («Фактический визит», cat-24 WON) per TM for the period."""
    return await _won_by_tm(
        bx,
        settings.companion_tm_category_id,
        settings.companion_meeting_stage_id,
        _meetings_cache,
        start,
        end,
    )


async def sold_deals_by_tm(
    bx: BitrixClient,
    start: datetime,
    end: datetime,
) -> dict[int, int]:
    """Bookings signed («купили», cat-2 «БРОНЬ ПОДПИСАН») per TM for the period.

    After the visit the TM deal moves to the sales funnel (cat 2) and is
    reassigned to the closer but keeps «Сотрудник ТМ»; a C2:WON transition is the
    sale, attributed back to the TM. Shares the join with :func:`_meetings_by_tm`.
    """
    if not settings.companion_sold_stage_id:
        return {}
    return await _won_by_tm(
        bx,
        settings.companion_sales_category_id,
        settings.companion_sold_stage_id,
        _sold_cache,
        start,
        end,
    )


async def conducted_meetings_by_tm(
    bx: BitrixClient,
    start: datetime,
    end: datetime,
) -> dict[int, int]:
    """Public view of :func:`_meetings_by_tm` for cross-module reuse.

    Conversions to «Фактический визит» per TM user id for the period (the team
    view surfaces these per manager). Shares the period cache with /day.
    """
    return await _meetings_by_tm(bx, start, end)


async def manager_ref(session: AsyncSession, bitrix_user_id: int) -> ManagerRef:
    """Public view of :func:`_manager_ref` (identity for cross-module reuse)."""
    return await _manager_ref(session, bitrix_user_id)


# (uid, "start/end") -> (expiry, {"YYYY-MM": conducted meetings}). One combined
# WON pull over the whole window serves the CR trend (vs one pull per month).
_won_month_cache: dict[tuple[int, str], tuple[float, dict[str, int]]] = {}


async def won_by_month_for_tm(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
) -> dict[str, int]:
    """Conducted meetings for one TM, bucketed by ``YYYY-MM``, in ONE pull.

    The CR trend needs the manager's «Фактический визит» (WON) count per month
    across a multi-month window. Doing :func:`conducted_meetings_by_tm` once per
    month re-pulls and re-looks-up each month separately; this instead makes a
    single ``crm.stagehistory.list`` pass over the whole window (carrying
    ``CREATED_TIME`` to bucket events by month) and a single deal lookup pass for
    the «Сотрудник ТМ» attribution — far fewer Bitrix round-trips. Counts each
    deal once per month it converted in.
    """
    s, e = start.date().isoformat(), end.date().isoformat()
    cache_key = (uid, f"{s}/{e}")
    hit = _won_month_cache.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    # deal id -> set of "YYYY-MM" it had a WON event in (a deal can convert once).
    deal_months: dict[int, set[str]] = {}
    cursor: int | None = 0
    while cursor is not None:
        env = await bx.call_raw(
            "crm.stagehistory.list",
            {
                "entityTypeId": 2,
                "filter": {
                    "CATEGORY_ID": settings.companion_tm_category_id,
                    "STAGE_ID": settings.companion_meeting_stage_id,
                    ">=CREATED_TIME": s,
                    "<CREATED_TIME": e,
                },
                "select": ["OWNER_ID", "CREATED_TIME"],
                "start": cursor,
            },
        )
        result = env.get("result") or {}
        items = result.get("items") if isinstance(result, dict) else result
        for it in items or []:
            created = _parse_dt(it.get("CREATED_TIME"))
            if created is None:
                continue
            month = f"{created.year:04d}-{created.month:02d}"
            deal_months.setdefault(int(it["OWNER_ID"]), set()).add(month)
        nxt = env.get("next")
        cursor = int(nxt) if nxt is not None else None

    field = settings.companion_tm_employee_field
    counts: dict[str, int] = {}
    ids = sorted(deal_months)
    for i in range(0, len(ids), 50):
        async for row in bx.list(
            "crm.deal.list",
            {"filter": {"ID": ids[i : i + 50]}, "select": ["ID", field]},
        ):
            tm = row.get(field)
            if not tm or str(tm) != str(uid):
                continue
            for month in deal_months[int(row["ID"])]:
                counts[month] = counts.get(month, 0) + 1

    expiry = time.monotonic() + settings.companion_analytics_cache_ttl_seconds
    _won_month_cache[cache_key] = (expiry, counts)
    return counts


async def count_list(
    bx: BitrixClient,
    method: str,
    filter_: dict[str, Any],
) -> int:
    """Public view of :func:`_count_list` — total rows for a list-method filter."""
    return await _count_list(bx, method, filter_)


# (stage_id, "start/end") -> (expiry, (distinct_by_assignee, rebooked_by_assignee)).
# One pull per stage+window serves both the funnel/meetings counts and the
# re-booking count; shared so a period is never pulled twice.
_outcomes_cache: dict[tuple[str, str], tuple[float, _StageOutcomes]] = {}


async def stage_outcomes_by_assignee(
    bx: BitrixClient,
    stage_id: str,
    start: datetime,
    end: datetime,
) -> _StageOutcomes:
    """Deals entering ``stage_id`` in the window, per assignee — distinct + rebooked.

    Returns ``(distinct, rebooked)``: ``distinct`` counts each deal once (the
    funnel/«назначено»/«недошли» numbers); ``rebooked`` counts only deals that
    entered the stage **2+ times** in the window (a re-booking — feeds
    «переназначились»). Both attributed by ``ASSIGNED_BY_ID``, correct for the
    pre-WON stages the TM still owns (unlike the WON stage, see
    :func:`conducted_meetings_by_tm`).
    """
    s, e = start.date().isoformat(), end.date().isoformat()
    cache_key = (stage_id, f"{s}/{e}")
    hit = _outcomes_cache.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    entries: dict[int, int] = {}  # deal id -> number of entries in window
    cursor: int | None = 0
    while cursor is not None:
        env = await bx.call_raw(
            "crm.stagehistory.list",
            {
                "entityTypeId": 2,
                "filter": {
                    "CATEGORY_ID": settings.companion_tm_category_id,
                    "STAGE_ID": stage_id,
                    ">=CREATED_TIME": s,
                    "<CREATED_TIME": e,
                },
                "select": ["OWNER_ID"],
                "start": cursor,
            },
        )
        result = env.get("result") or {}
        items = result.get("items") if isinstance(result, dict) else result
        for it in items or []:
            owner = int(it["OWNER_ID"])
            entries[owner] = entries.get(owner, 0) + 1
        nxt = env.get("next")
        cursor = int(nxt) if nxt is not None else None

    distinct: dict[int, int] = {}
    rebooked: dict[int, int] = {}
    ids = sorted(entries)
    for i in range(0, len(ids), 50):
        async for row in bx.list(
            "crm.deal.list",
            {"filter": {"ID": ids[i : i + 50]}, "select": ["ID", "ASSIGNED_BY_ID"]},
        ):
            assignee = row.get("ASSIGNED_BY_ID")
            if not assignee or str(assignee) == "0":
                continue
            uid = int(assignee)
            distinct[uid] = distinct.get(uid, 0) + 1
            if entries[int(row["ID"])] >= 2:
                rebooked[uid] = rebooked.get(uid, 0) + 1

    outcomes: _StageOutcomes = (distinct, rebooked)
    expiry = time.monotonic() + settings.companion_day_cache_ttl_seconds
    _outcomes_cache[cache_key] = (expiry, outcomes)
    return outcomes


# (stage_id, "start/end") -> (expiry, {assignee_user_id: deals entered}). One pull
# per stage+window serves every manager (team views hit it once per TTL).
_entrants_cache: dict[tuple[str, str], tuple[float, dict[int, int]]] = {}


async def _stage_entrants_by_assignee(
    bx: BitrixClient,
    stage_id: str | list[str],
    start: datetime,
    end: datetime,
) -> dict[int, int]:
    """Distinct deals that *entered* ``stage_id`` in the window, per assignee.

    ``stage_id`` may be one stage or a list (counted as a set — a deal entering
    two of them in the window is one deal). Used for "назначено сегодня" (the
    booking stage) and "дожать до встречи" (any hot pre-booking stage entered
    today). Unlike the WON stage (whose deal is reassigned to the closer),
    pre-meeting stages rest with the TM, so ``ASSIGNED_BY_ID`` is correct.
    """
    stages = [stage_id] if isinstance(stage_id, str) else sorted(stage_id)
    s, e = start.date().isoformat(), end.date().isoformat()
    cache_key = (",".join(stages), f"{s}/{e}")
    hit = _entrants_cache.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    deal_ids: set[int] = set()
    cursor: int | None = 0
    while cursor is not None:
        env = await bx.call_raw(
            "crm.stagehistory.list",
            {
                "entityTypeId": 2,
                "filter": {
                    "CATEGORY_ID": settings.companion_tm_category_id,
                    "STAGE_ID": stages,
                    ">=CREATED_TIME": s,
                    "<CREATED_TIME": e,
                },
                "select": ["OWNER_ID"],
                "start": cursor,
            },
        )
        result = env.get("result") or {}
        items = result.get("items") if isinstance(result, dict) else result
        deal_ids.update(int(it["OWNER_ID"]) for it in items or [])
        nxt = env.get("next")
        cursor = int(nxt) if nxt is not None else None

    counts: dict[int, int] = {}
    ids = sorted(deal_ids)
    for i in range(0, len(ids), 50):
        async for row in bx.list(
            "crm.deal.list",
            {"filter": {"ID": ids[i : i + 50]}, "select": ["ID", "ASSIGNED_BY_ID"]},
        ):
            assignee = row.get("ASSIGNED_BY_ID")
            if assignee and str(assignee) != "0":
                counts[int(assignee)] = counts.get(int(assignee), 0) + 1

    expiry = time.monotonic() + settings.companion_day_cache_ttl_seconds
    _entrants_cache[cache_key] = (expiry, counts)
    return counts


async def stage_entrants_by_assignee(
    bx: BitrixClient,
    stage_id: str | list[str],
    start: datetime,
    end: datetime,
) -> dict[int, int]:
    """Public view of :func:`_stage_entrants_by_assignee` for cross-module reuse.

    Distinct deals that entered ``stage_id`` (one stage or a set) in the window,
    per assignee — e.g. the analytics «Недозвон» bar. Shares the period cache.
    """
    return await _stage_entrants_by_assignee(bx, stage_id, start, end)


def _today_window() -> tuple[datetime, datetime]:
    """[midnight today, midnight tomorrow) in the report timezone."""
    tz = ZoneInfo(settings.report_timezone)
    now = datetime.now(tz=tz)
    start = datetime(now.year, now.month, now.day, tzinfo=tz)
    return start, start + timedelta(days=1)


async def _planned_calls_today(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
) -> int:
    """«Записано на сегодня» — *open* call activities the manager planned today.

    ``COMPLETED='N'`` is essential: Bitrix telephony auto-creates a *completed*
    call activity for every call that actually happens, so without it the count
    is "planned calls + every call already logged today" — heavily inflated. This
    matches Bitrix's own «Дела на сегодня» counter (planned, not done).
    """
    return await _count_list(
        bx,
        "crm.activity.list",
        {
            "RESPONSIBLE_ID": uid,
            "TYPE_ID": settings.companion_call_activity_type_id,
            "COMPLETED": "N",
            ">=DEADLINE": start.isoformat(),
            "<DEADLINE": end.isoformat(),
        },
    )


async def _overdue_tasks(
    bx: BitrixClient,
    uid: int,
    day_start: datetime,
    day_end: datetime,
) -> int:
    """«Просроченных» — tasks due on the day whose deadline has already passed.

    Incomplete activities (``COMPLETED=N``) with ``DEADLINE`` in ``[day_start,
    upper)``. For today ``upper`` is *now* (due today, already past); for a past
    day it is the day's end — what was left undone that day. Day-scoped, like the
    other tiles.
    """
    now = datetime.now(tz=ZoneInfo(settings.report_timezone))
    upper = min(day_end, now)
    if upper <= day_start:  # a future day — nothing overdue yet
        return 0
    return await _count_list(
        bx,
        "crm.activity.list",
        {
            "RESPONSIBLE_ID": uid,
            "COMPLETED": "N",
            ">=DEADLINE": day_start.isoformat(),
            "<DEADLINE": upper.isoformat(),
        },
    )


async def _overdue_task_items(
    bx: BitrixClient,
    uid: int,
    day_start: datetime,
    day_end: datetime,
    limit: int,
) -> list[DayTaskItem]:
    """A few example overdue tasks (same window as :func:`_overdue_tasks`).

    The oldest-due first, each linked to the deal/contact it hangs off of, so the
    «Просроченные задачи» queue can list examples under its counter.
    """
    now = datetime.now(tz=ZoneInfo(settings.report_timezone))
    upper = min(day_end, now)
    if upper <= day_start:
        return []
    env = await bx.call_raw(
        "crm.activity.list",
        {
            "filter": {
                "RESPONSIBLE_ID": uid,
                "COMPLETED": "N",
                ">=DEADLINE": day_start.isoformat(),
                "<DEADLINE": upper.isoformat(),
            },
            "order": {"DEADLINE": "ASC"},
            "select": ["ID", "SUBJECT", "DEADLINE", "OWNER_ID", "OWNER_TYPE_ID"],
        },
    )
    items: list[DayTaskItem] = []
    for r in (env.get("result") or [])[:limit]:
        owner_type = int(r.get("OWNER_TYPE_ID") or 0)
        owner_id = int(r["OWNER_ID"]) if r.get("OWNER_ID") else None
        entity = _ACTIVITY_OWNER_ENTITY.get(owner_type)
        items.append(
            DayTaskItem(
                activity_id=int(r["ID"]),
                subject=str(r.get("SUBJECT") or "Задача"),
                deadline=_parse_dt(r.get("DEADLINE")),
                bitrix_url=crm_card_url(entity, owner_id),
            ),
        )
    return items


async def team_overdue_tasks(
    bx: BitrixClient,
    responsible_names: dict[int, str | None],
    dept_bitrix_id: int,
    dept_name: str | None,
    now: datetime,
    limit: int,
) -> tuple[list[OverdueTaskItem], bool]:
    """All incomplete, past-deadline activities for a team, oldest-due first.

    Filters ``COMPLETED='N'`` with ``DEADLINE`` in ``[floor, now)`` over the
    team's ``responsible_names`` (RESPONSIBLE_ID -> display name), ordered by
    ``DEADLINE`` ascending — the «Просроченные задачи» РОП queue. Returns
    ``(items, truncated)``: ``truncated`` is True when more than ``limit``
    matched (the list is capped at ``limit``). The floor drops Bitrix's
    zero-date (no-deadline) activities.
    """
    if not responsible_names:
        return [], False
    raw: list[dict[str, Any]] = []
    async for r in bx.list(
        "crm.activity.list",
        {
            "filter": {
                "RESPONSIBLE_ID": sorted(responsible_names),
                "COMPLETED": "N",
                ">=DEADLINE": _DEADLINE_FLOOR,
                "<DEADLINE": now.isoformat(),
            },
            "order": {"DEADLINE": "ASC"},
            "select": [
                "ID",
                "SUBJECT",
                "DEADLINE",
                "OWNER_ID",
                "OWNER_TYPE_ID",
                "RESPONSIBLE_ID",
            ],
        },
        max_items=limit + 1,  # one extra row just to detect truncation
    ):
        raw.append(r)
    truncated = len(raw) > limit
    items: list[OverdueTaskItem] = []
    for r in raw[:limit]:
        owner_type = int(r.get("OWNER_TYPE_ID") or 0)
        owner_id = int(r["OWNER_ID"]) if r.get("OWNER_ID") else None
        entity = _ACTIVITY_OWNER_ENTITY.get(owner_type)
        uid = int(r["RESPONSIBLE_ID"]) if r.get("RESPONSIBLE_ID") else None
        items.append(
            OverdueTaskItem(
                activity_id=int(r["ID"]),
                subject=str(r.get("SUBJECT") or "Задача"),
                deadline=_parse_dt(r.get("DEADLINE")),
                manager=ManagerRef(
                    bitrix_user_id=uid or 0,
                    name=responsible_names.get(uid) if uid else None,
                    department_id=dept_bitrix_id,
                    department_name=dept_name,
                ),
                bitrix_url=crm_card_url(entity, owner_id),
            ),
        )
    return items, truncated


async def _talk_time_today(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
) -> int:
    """«Время на линии» — total answered-call talk seconds today (Bitrix telephony).

    Sums ``CALL_DURATION`` over the manager's answered calls (every call,
    analyzed or not), so it reflects real time on the phone.
    """
    total = 0
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
            total += int(row.get("CALL_DURATION") or 0)
    return total


async def _today_metrics(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
    deals: list[dict[str, Any]] | None = None,
) -> DayToday:
    """«Важные цифры дня» — headline numbers for the Мой день card.

    Resilient: a failing Bitrix read for any tile degrades that field to None
    (UI shows "—") rather than failing the whole day view. Most tiles are a
    single day in the report timezone (today by default, or a past day when the
    caller passes ``date``); ``in_qual`` is the exception — it is a *current*
    snapshot of the manager's open pipeline (deals resting at the qualified
    stage = clients «в квале»), computed from the already-fetched ``deals`` list
    (no extra Bitrix call), so it stays "на сейчас" like the queues.
    """
    qual_stage = settings.companion_qualified_stage_id
    in_qual = sum(
        1 for d in (deals or []) if str(d.get("STAGE_ID") or "") == qual_stage
    )
    try:
        planned = await _planned_calls_today(bx, uid, start, end)
    except BitrixError:
        planned = None
    try:
        meetings_set: int | None = (
            await _stage_entrants_by_assignee(
                bx,
                settings.companion_meeting_set_stage_id,
                start,
                end,
            )
        ).get(uid, 0)
    except BitrixError:
        meetings_set = None
    try:
        talk = await _talk_time_today(bx, uid, start, end)
    except BitrixError:
        talk = None
    try:
        push: int | None = (
            await _stage_entrants_by_assignee(bx, _HOT_STAGES, start, end)
        ).get(uid, 0)
    except BitrixError:
        push = None
    try:
        closed: int | None = (await _meetings_by_tm(bx, start, end)).get(uid, 0)
    except BitrixError:
        closed = None
    try:
        overdue: int | None = await _overdue_tasks(bx, uid, start, end)
    except BitrixError:
        overdue = None
    return DayToday(
        planned_calls=planned,
        meetings_set=meetings_set,
        talk_time_sec=talk,
        push_to_meeting=push,
        in_qual=in_qual,
        deals_closed=closed,
        overdue=overdue,
    )


async def _money(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
) -> MoneyAxis:
    """Period money axis from real Zvandau counts (conversion = meetings ÷ leads).

    Meetings = stage-history WON transitions attributed via «Сотрудник ТМ» (see
    ``_meetings_by_tm``); leads = deals created in the period. ``plan_pct`` uses
    the configured policy target (not Bitrix). ``crm_discipline_pct`` stays
    null — not trustworthy yet.
    """
    cat = settings.companion_tm_category_id
    s, e = start.date().isoformat(), end.date().isoformat()
    base = {"CATEGORY_ID": cat, "ASSIGNED_BY_ID": uid}
    meetings = (await _meetings_by_tm(bx, start, end)).get(uid, 0)
    leads = await _count(bx, {**base, ">=DATE_CREATE": s, "<DATE_CREATE": e})

    conversion = round(meetings / leads * 100, 1) if leads else None
    target = settings.companion_plan_target_meetings
    plan = round(meetings / target * 100, 1) if target else None
    return MoneyAxis(
        status="live" if leads or meetings else "not_available",
        conversion_pct=conversion,
        plan_pct=plan,
        crm_discipline_pct=None,
        meetings=meetings,
        leads_processed=leads,
        gates={"plan_ok": (plan or 0) >= 60} if plan is not None else None,
    )


def _bucket_of(d: dict[str, Any], stale_before: datetime) -> str | None:
    """The Мой день queue a deal falls in: meetings | no_answer | cooling | None.

    Its stage's bucket, except a neutral-stage deal idle past ``stale_before``
    counts as cooling (остывает). This is the single mapping the counters
    (:func:`_compute_stats`), the action tags, and the example picker all share.
    """
    stage = str(d.get("STAGE_ID") or "")
    _, _, bucket = _STAGE_SIGNALS.get(stage, _DEFAULT_SIGNAL)
    if bucket is None:
        last = _parse_dt(d.get("LAST_ACTIVITY_TIME"))
        if last is not None and last < stale_before:
            return "cooling"
    return bucket


def _is_no_task(d: dict[str, Any], with_task_ids: set[int] | None) -> bool:
    """Whether a deal is a «брошенная» card without a next task.

    ``with_task_ids`` is the set of deals with an open activity; None means the
    activity read was unavailable, so no-task can't be asserted (returns False).
    """
    return with_task_ids is not None and int(d["ID"]) not in with_task_ids


def _compute_stats(
    deals: list[dict[str, Any]],
    now: datetime,
    with_task_ids: set[int] | None = None,
) -> DayStats:
    """The counters over the *whole* open pipeline (not the shown slice).

    ``with_task_ids`` (deals that have an open activity) drives the «Без задачи»
    counter; None (the activity read was unavailable) leaves ``no_task`` null so
    the UI shows "—" rather than a misleading zero.
    """
    meetings = no_answer = cooling = no_task = 0
    stale_before = now - timedelta(days=_STALE_DAYS)
    for d in deals:
        bucket = _bucket_of(d, stale_before)
        if bucket == "meetings":
            meetings += 1
        elif bucket == "no_answer":
            no_answer += 1
        elif bucket == "cooling":
            cooling += 1
        if _is_no_task(d, with_task_ids):
            no_task += 1
    return DayStats(
        meetings=meetings,
        no_answer=no_answer,
        cooling=cooling,
        no_task=no_task if with_task_ids is not None else None,
    )


def _select_action_deals(
    deals: list[dict[str, Any]],
    now: datetime,
    cap: int,
    with_task_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Which deals to expose as actions — every deal that belongs to a queue.

    Actions feed only the «Займись сейчас» queues (each expandable to its full
    list), so neutral deals are dropped — *unless* they have no open task, which
    lands them in the «Без задачи» selection bucket (a deal already in a stage
    bucket keeps it and carries the no_task flag separately). ``deals`` arrive
    stalest-first, so a plain ``deals[:cap]`` would let the big cooling bucket
    crowd out the freshly-active no_answer one; we round-robin across buckets
    instead, so under the cap every queue keeps a fair share. Within a bucket the
    order stays stalest-first — except no_answer (missed calls), which we flip to
    freshest-first so the hottest just-active leads surface to call back
    immediately. Below the cap every queued deal is returned.
    """
    stale_before = now - timedelta(days=_STALE_DAYS)
    by_bucket: dict[str, list[dict[str, Any]]] = {}
    for d in deals:
        bucket = _bucket_of(d, stale_before)
        if bucket is None and _is_no_task(d, with_task_ids):
            bucket = "no_task"
        if bucket is not None:
            by_bucket.setdefault(bucket, []).append(d)
    if "no_answer" in by_bucket:
        by_bucket["no_answer"].reverse()
    chosen: list[dict[str, Any]] = []
    row = 0
    while len(chosen) < cap:
        added = False
        for lst in by_bucket.values():
            if row < len(lst):
                chosen.append(lst[row])
                added = True
                if len(chosen) >= cap:
                    break
        if not added:
            break
        row += 1
    return chosen


def _build_actions(
    deals: list[dict[str, Any]],
    contacts: dict[int, dict[str, Any]],
    now: datetime,
    with_task_ids: set[int] | None = None,
) -> list[DayActionItem]:
    """The "кому звонить" cards for the selected deal slice.

    Each carries its ``queue`` bucket (:func:`_bucket_of`) and a ``no_task`` flag
    (:func:`_is_no_task`, orthogonal to the bucket), so the "Займись сейчас"
    queues can list a few example deals under each counter.
    """
    stale_before = now - timedelta(days=_STALE_DAYS)
    actions: list[DayActionItem] = []
    for d in deals:
        stage = str(d.get("STAGE_ID") or "")
        reason, heat, _ = _STAGE_SIGNALS.get(stage, _DEFAULT_SIGNAL)
        contact = contacts.get(int(d["CONTACT_ID"])) if d.get("CONTACT_ID") else None
        deal_id = int(d["ID"])
        actions.append(
            DayActionItem(
                deal_id=deal_id,
                client_name=_name_of(contact) if contact else None,
                phone=_phone_of(contact) if contact else None,
                stage_id=stage,
                reason=reason,
                heat=heat,
                queue=_bucket_of(d, stale_before),
                no_task=_is_no_task(d, with_task_ids),
                last_activity_at=_parse_dt(d.get("LAST_ACTIVITY_TIME")),
                bitrix_url=crm_card_url("DEAL", deal_id),
            ),
        )
    return actions


def _day_window(date: str | None) -> tuple[datetime, datetime, str]:
    """«Важные цифры дня» window: today by default, or a single past ``date``.

    ``date`` must be a single ``YYYY-MM-DD`` day (not a month or a range) — the
    tiles are inherently day-scoped. Returns ``(start, end, day_label)`` where the
    label is ``"today"`` for the default so it never collides with a dated key.
    """
    if date is None:
        start, end = _today_window()
        return start, end, "today"
    if ".." in date or date.count("-") != 2:
        raise okk.PeriodError(
            f"date must be a single YYYY-MM-DD day (got {date!r})",
        )
    start, end, label = okk.parse_period(date)
    return start, end, label


async def get_day(
    session: AsyncSession,
    bitrix_user_id: int,
    period: str | None,
    date: str | None = None,
) -> DayView:
    """Live Мой день for a manager (Bitrix user id).

    ``period`` (YYYY-MM) drives the month money axis; ``date`` (YYYY-MM-DD, default
    today) drives the «Важные цифры дня» tiles so a manager can review a past day.
    The open-pipeline queues/actions are always the *current* pipeline.
    """
    start, end, label = okk.parse_period(period)
    day_start, day_end, day_label = _day_window(date)
    cache_key = (bitrix_user_id, label, day_label)
    hit = _cache.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    manager = await _manager_ref(session, bitrix_user_id)
    now = datetime.now(tz=UTC)
    # Postgres reads, independent of the live Bitrix pull below — they survive a
    # Bitrix outage and populate the «Отказы не по делу» and «По оценке ОКК» queues.
    audit_failed = await _audit_failed_items(
        session, bitrix_user_id, settings.companion_day_audit_max_items
    )
    no_meeting = await _no_meeting_items(
        session,
        bitrix_user_id,
        start,
        end,
        settings.companion_day_no_meeting_max_items,
    )

    try:
        async with BitrixClient() as bx:
            try:
                await _fill_client_names(bx, no_meeting)
            except BitrixError as exc:
                # Cosmetic read: the cards still carry the phone to dial. Never let
                # a failed name lookup collapse the whole day view.
                logger.warning("Callback-queue names unresolved: {e}", e=exc)
            deals = await _open_deals(bx, bitrix_user_id)
            try:
                with_task_ids: set[int] | None = await _deals_with_open_task(
                    bx,
                    {int(d["ID"]) for d in deals},
                )
            except BitrixError:
                with_task_ids = None  # «Без задачи» degrades to "—", rest survives
            action_deals = _select_action_deals(
                deals,
                now,
                settings.companion_day_max_actions,
                with_task_ids,
            )
            contact_ids = {
                int(d["CONTACT_ID"]) for d in action_deals if d.get("CONTACT_ID")
            }
            contacts = await _contacts(bx, contact_ids)
            money = await _money(bx, bitrix_user_id, start, end)
            today = await _today_metrics(bx, bitrix_user_id, day_start, day_end, deals)
            overdue_tasks = await _overdue_task_items(
                bx,
                bitrix_user_id,
                day_start,
                day_end,
                settings.companion_day_max_actions,
            )
    except BitrixError as exc:
        logger.warning(
            "Day view Bitrix read failed for {uid}: {e}",
            uid=bitrix_user_id,
            e=exc,
        )
        return DayView(
            manager=manager,
            period=label,
            data_ready=False,
            actions=[],
            stats=DayStats(meetings=0, no_answer=0, cooling=0),
            money=MoneyAxis(),
            audit_failed=audit_failed,
            no_meeting=no_meeting,
        )

    stats = _compute_stats(deals, now, with_task_ids)
    actions = _build_actions(action_deals, contacts, now, with_task_ids)
    data_ready = bool(deals) or bool(money.meetings)
    view = DayView(
        manager=manager,
        period=label,
        data_ready=data_ready,
        actions=actions,
        stats=stats,
        money=money,
        today=today,
        overdue_tasks=overdue_tasks,
        audit_failed=audit_failed,
        no_meeting=no_meeting,
    )
    expiry = time.monotonic() + settings.companion_day_cache_ttl_seconds
    _cache[cache_key] = (expiry, view)
    return view
