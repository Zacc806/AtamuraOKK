"""Live "Мой день" read-through over the Bitrix cat-0 (Телемаркетинг) funnel.

Unlike the rest of ``/api/v1`` (which reads OKK's Postgres), the day view reads
**straight through to Bitrix** per request (short TTL cache): it is an inherently
real-time screen ("кому звонить сейчас", "встречи сегодня") and the data lives in
the TM's own deal pipeline, owned by them via ``ASSIGNED_BY_ID``. OKK still owns
the Bitrix gateway, so the companion stays a thin consumer.

Trust boundary (see docs/companion-day.md): per-TM meeting/conversion attribution
depends on the Bitrix data-cleanup gate. When a manager has no live pipeline the
view returns ``data_ready=False`` so the UI shows "данные готовятся", never fake
numbers.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.db.models.department import Department
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.settings import settings
from AtamuraOKK.web.api.v1 import okk
from AtamuraOKK.web.api.v1.schemas import (
    DayActionItem,
    DayStats,
    DayView,
    ManagerRef,
    MoneyAxis,
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

# (uid, period_label) -> (monotonic expiry, DayView). Tiny in-process TTL cache so
# rapid re-opens / tab switches don't hammer Bitrix.
_cache: dict[tuple[int, str], tuple[float, DayView]] = {}


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


async def _count(bx: BitrixClient, filter_: dict[str, Any]) -> int:
    """Total rows for a deal filter, via the list envelope (no paging)."""
    env = await bx.call_raw(
        "crm.deal.list",
        {"filter": filter_, "select": ["ID"]},
    )
    total = env.get("total")
    return int(total) if total is not None else len(env.get("result") or [])


async def _money(
    bx: BitrixClient,
    uid: int,
    start: datetime,
    end: datetime,
) -> MoneyAxis:
    """Period money axis from real cat-0 counts (conversion = meetings ÷ leads).

    Meetings = deals that reached the "Фактический визит" stage in the period;
    leads = deals created in the period. ``plan_pct`` uses the configured policy
    target (not Bitrix). ``crm_discipline_pct`` stays null — not trustworthy yet.
    """
    cat = settings.companion_tm_category_id
    s, e = start.date().isoformat(), end.date().isoformat()
    base = {"CATEGORY_ID": cat, "ASSIGNED_BY_ID": uid}
    meetings = await _count(
        bx,
        {
            **base,
            "STAGE_ID": settings.companion_meeting_stage_id,
            ">=CLOSEDATE": s,
            "<CLOSEDATE": e,
        },
    )
    leads = await _count(bx, {**base, ">=DATE_CREATE": s, "<DATE_CREATE": e})

    conversion = round(meetings / leads * 100, 1) if leads else None
    target = settings.companion_plan_target_meetings
    plan = round(meetings / target * 100, 1) if target else None
    return MoneyAxis(
        status="live" if leads else "not_available",
        conversion_pct=conversion,
        plan_pct=plan,
        crm_discipline_pct=None,
        meetings=meetings,
        leads_processed=leads,
        gates={"plan_ok": (plan or 0) >= 60} if plan is not None else None,
    )


def _compute_stats(deals: list[dict[str, Any]], now: datetime) -> DayStats:
    """The three counters over the *whole* open pipeline (not the shown slice)."""
    meetings = no_answer = cooling = 0
    stale_before = now - timedelta(days=_STALE_DAYS)
    for d in deals:
        stage = str(d.get("STAGE_ID") or "")
        _, _, bucket = _STAGE_SIGNALS.get(stage, _DEFAULT_SIGNAL)
        if bucket == "meetings":
            meetings += 1
        elif bucket == "no_answer":
            no_answer += 1
        elif bucket == "cooling":
            cooling += 1
        else:
            last = _parse_dt(d.get("LAST_ACTIVITY_TIME"))
            if last is not None and last < stale_before:
                cooling += 1  # idle too long -> remind even if its stage is neutral
    return DayStats(meetings=meetings, no_answer=no_answer, cooling=cooling)


def _build_actions(
    deals: list[dict[str, Any]],
    contacts: dict[int, dict[str, Any]],
) -> list[DayActionItem]:
    """The "кому звонить" cards for the shown (capped) deal slice."""
    actions: list[DayActionItem] = []
    for d in deals:
        stage = str(d.get("STAGE_ID") or "")
        reason, heat, _ = _STAGE_SIGNALS.get(stage, _DEFAULT_SIGNAL)
        contact = contacts.get(int(d["CONTACT_ID"])) if d.get("CONTACT_ID") else None
        actions.append(
            DayActionItem(
                deal_id=int(d["ID"]),
                client_name=_name_of(contact) if contact else None,
                phone=_phone_of(contact) if contact else None,
                stage_id=stage,
                reason=reason,
                heat=heat,
                last_activity_at=_parse_dt(d.get("LAST_ACTIVITY_TIME")),
            ),
        )
    return actions


async def get_day(
    session: AsyncSession,
    bitrix_user_id: int,
    period: str | None,
) -> DayView:
    """Live Мой день for a manager (Bitrix user id) in a YYYY-MM period."""
    start, end, label = okk.parse_period(period)
    cache_key = (bitrix_user_id, label)
    hit = _cache.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    manager = await _manager_ref(session, bitrix_user_id)
    now = datetime.now(tz=UTC)

    try:
        async with BitrixClient() as bx:
            deals = await _open_deals(bx, bitrix_user_id)
            action_deals = deals[: settings.companion_day_max_actions]
            contact_ids = {
                int(d["CONTACT_ID"]) for d in action_deals if d.get("CONTACT_ID")
            }
            contacts = await _contacts(bx, contact_ids)
            money = await _money(bx, bitrix_user_id, start, end)
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
        )

    stats = _compute_stats(deals, now)
    actions = _build_actions(action_deals, contacts)
    data_ready = bool(deals) or bool(money.meetings)
    view = DayView(
        manager=manager,
        period=label,
        data_ready=data_ready,
        actions=actions,
        stats=stats,
        money=money,
    )
    expiry = time.monotonic() + settings.companion_day_cache_ttl_seconds
    _cache[cache_key] = (expiry, view)
    return view
