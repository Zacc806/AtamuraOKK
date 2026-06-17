"""Read queries backing the companion API.

Everything is sourced from the ``call_scores_latest`` / ``call_criteria_latest``
views (the read contract) plus the ``managers`` / ``departments`` tables for
identity. Nothing exposes the internal status enum. The single write path is
``assign_manager_department`` (key issuance by a scoped head ties the manager
to the head's department); everything else is read-only.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.bitrix import BitrixClient, BitrixError, crm_card_url
from AtamuraOKK.db.models.appeal import (
    APPEAL_ACCEPTED,
    APPEAL_PENDING,
    Appeal,
)
from AtamuraOKK.db.models.department import Department
from AtamuraOKK.db.models.enums import CompanionRole
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.db.models.meeting import Meeting
from AtamuraOKK.db.models.rubric_version import RubricVersion
from AtamuraOKK.settings import settings
from AtamuraOKK.web.api.v1 import day, okk
from AtamuraOKK.web.api.v1.auth import CompanionIdentity
from AtamuraOKK.web.api.v1.schemas import (
    AppealView,
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
    TranscriptBlock,
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


def _transcript_blocks(
    segments: Any,
    full_text: str | None,
) -> list[TranscriptBlock]:
    """Speaker-labeled blocks from a transcript row.

    Segments arrive channel-grouped (all agent, then all customer), so
    consecutive same-speaker segments coalesce into one block. Falls back to
    ``full_text`` as a single block when segments are absent.
    """
    if isinstance(segments, str):
        try:
            segments = json.loads(segments)
        except json.JSONDecodeError:
            segments = None
    blocks: list[TranscriptBlock] = []
    if isinstance(segments, list):
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_text = str(seg.get("text") or "").strip()
            if not seg_text:
                continue
            speaker = str(seg.get("speaker") or "unknown")
            if blocks and blocks[-1].speaker == speaker:
                blocks[-1].text += " " + seg_text
            else:
                blocks.append(TranscriptBlock(speaker=speaker, text=seg_text))
    if not blocks and full_text and full_text.strip():
        blocks.append(TranscriptBlock(speaker="unknown", text=full_text.strip()))
    return blocks


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


async def _score_overrides(
    session: AsyncSession,
    call_ids: Sequence[int | None],
) -> dict[int, float]:
    """``call_id → corrected percent`` from the latest accepted appeal override.

    A head re-checking an appeal may record a corrected percent; that value
    supersedes the LLM percent everywhere a score is shown. Kept out of the
    ``call_scores_latest`` view on purpose, so the official QA reports (which
    read the same view) stay the model's verdict — only the companion read
    layer prefers the human override.
    """
    ids = sorted({c for c in call_ids if c is not None})
    if not ids:
        return {}
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT ON (call_id) call_id, override_percent "
                "FROM appeals "
                "WHERE call_id = ANY(:ids) "
                "AND status = 'accepted' AND override_percent IS NOT NULL "
                "ORDER BY call_id, reviewed_at DESC NULLS LAST, id DESC",
            ),
            {"ids": ids},
        )
    ).all()
    return {r.call_id: float(r.override_percent) for r in rows}


def _with_overrides(rows: Sequence[Any], overrides: dict[int, float]) -> list[Any]:
    """Replace each overridden row's ``percent``/``zone`` with the РОП correction.

    Rows must carry ``call_id``. Untouched rows pass through as-is; an overridden
    row becomes a namespace clone whose ``percent`` is the corrected value and
    whose ``zone`` is re-derived from it (so ``okk_5`` and zone counting stay
    consistent with the corrected number).
    """
    if not overrides:
        return list(rows)
    out: list[Any] = []
    for r in rows:
        corrected = overrides.get(r.call_id)
        if corrected is None:
            out.append(r)
            continue
        data = dict(r._mapping)  # noqa: SLF001 — read-only copy of a Core row
        data["percent"] = corrected
        data["zone"] = okk.zone_for(corrected)
        out.append(SimpleNamespace(**data))
    return out


async def _apply_score_overrides(
    session: AsyncSession,
    rows: Sequence[Any],
) -> list[Any]:
    """Fetch and fold the РОП score overrides for a batch of score rows."""
    overrides = await _score_overrides(session, [r.call_id for r in rows])
    return _with_overrides(rows, overrides)


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
                    "SELECT call_id, percent, zone, is_qualification_call "
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


def _is_placeholder_name(department: Department) -> bool:
    """True while a department row still carries its get-or-create stub name."""
    return (
        not department.name or department.name == f"Department {department.bitrix_id}"
    )


async def _bitrix_department_names() -> dict[int, str]:
    """Live read-only ``department.get`` → ``{bitrix_dept_id: name}``.

    Degrades to an empty map on any Bitrix problem (missing ``department``
    scope, webhook unset/unreachable) — callers keep the placeholder name.
    """
    names: dict[int, str] = {}
    try:
        async with BitrixClient() as bx:
            async for row in bx.list("department.get"):
                dept_id, name = row.get("ID"), row.get("NAME")
                if dept_id is not None and name:
                    names[int(dept_id)] = str(name)
    except (BitrixError, ValueError):
        return {}
    return names


async def list_departments(session: AsyncSession) -> list[DepartmentRef]:
    """All known departments (Bitrix id + name) for the access-management UI.

    Names in OKK's ``departments`` table are stubs until synced, so this lazily
    backfills real Bitrix department names (``department.get``) once and
    persists them, degrading to the stored stub when Bitrix is unreachable.
    """
    departments = list(
        (
            await session.scalars(
                select(Department).where(Department.bitrix_id.is_not(None)),
            )
        ).all(),
    )
    placeholders: dict[int, Department] = {
        bid: d
        for d in departments
        if (bid := d.bitrix_id) is not None and _is_placeholder_name(d)
    }
    if placeholders:
        names = await _bitrix_department_names()
        for bitrix_id, dept in placeholders.items():
            real = names.get(bitrix_id)
            if real:
                dept.name = real
        await session.flush()
    departments.sort(key=lambda d: (d.name or "").casefold())
    return [
        DepartmentRef(bitrix_id=bid, name=d.name)
        for d in departments
        if (bid := d.bitrix_id) is not None
    ]


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


async def get_manager_department_bitrix_id(
    session: AsyncSession,
    bitrix_user_id: int | None,
) -> int | None:
    """The Bitrix department id a manager belongs to, or None if unknown."""
    if bitrix_user_id is None:
        return None
    return await session.scalar(
        select(Department.bitrix_id)
        .join(Manager, Manager.department_id == Department.id)
        .where(Manager.bitrix_user_id == bitrix_user_id),
    )


async def _ensure_department_by_bitrix_id(
    session: AsyncSession,
    bitrix_dept_id: int,
) -> int:
    """Local id of a department, get-or-create by Bitrix id (placeholder name)."""
    department = await session.scalar(
        select(Department).where(Department.bitrix_id == bitrix_dept_id),
    )
    if department is None:
        department = Department(
            bitrix_id=bitrix_dept_id,
            name=f"Department {bitrix_dept_id}",
        )
        session.add(department)
        await session.flush()
    return department.id


async def assign_manager_department(
    session: AsyncSession,
    bitrix_user_id: int,
    department_bitrix_id: int,
    display_name: str | None,
) -> None:
    """Tie a manager to a department — the cabinet's word over Bitrix's.

    Used when a scoped head issues a manager key: get-or-creates the
    ``managers`` row, points it at the head's department (overriding a
    Bitrix-derived one) and marks it ``enriched`` so ingestion never
    re-derives the department — which also freezes its email/active backfill
    (accepted: the head's assignment is authoritative).
    """
    manager = await session.scalar(
        select(Manager).where(Manager.bitrix_user_id == bitrix_user_id),
    )
    if manager is None:
        manager = Manager(bitrix_user_id=bitrix_user_id, name=display_name)
        session.add(manager)
    manager.department_id = await _ensure_department_by_bitrix_id(
        session,
        department_bitrix_id,
    )
    manager.enriched = True
    await session.flush()


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
    rows = await _apply_score_overrides(session, rows)
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
                "is_qualification_call, summary, crm_entity_type, crm_entity_id "
                "FROM call_scores_latest "
                "WHERE manager_bitrix_user_id = :uid "
                "AND (CAST(:since AS timestamptz) IS NULL "
                "OR started_at >= CAST(:since AS timestamptz)) "
                "ORDER BY started_at DESC NULLS LAST LIMIT :limit",
            ),
            {"uid": bitrix_user_id, "since": since, "limit": limit},
        )
    ).all()
    rows = await _apply_score_overrides(session, rows)
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
            bitrix_url=crm_card_url(r.crm_entity_type, r.crm_entity_id),
        )
        for r in rows
    ]


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


#: CRM card path segment (as in ``…/crm/<type>/details/<id>/``) → the
#: ``crm_entity_type`` calls are stored under.
CRM_ENTITY_TYPES = {
    "deal": "DEAL",
    "contact": "CONTACT",
    "company": "COMPANY",
    "lead": "LEAD",
}


def _add_pair(pairs: set[tuple[str, int]], entity_type: str, raw_id: Any) -> None:
    value = _as_int(raw_id)
    if value:  # Bitrix sends "0" for an absent link — treat as none.
        pairs.add((entity_type, value))


async def _resolve_deal(
    bx: BitrixClient,
    pairs: set[tuple[str, int]],
    deal_id: int,
) -> None:
    deal = await bx.call("crm.deal.get", {"id": deal_id})
    if deal:
        _add_pair(pairs, "COMPANY", deal.get("COMPANY_ID"))
        _add_pair(pairs, "CONTACT", deal.get("CONTACT_ID"))
    for item in (await bx.call("crm.deal.contact.items.get", {"id": deal_id})) or []:
        _add_pair(pairs, "CONTACT", item.get("CONTACT_ID"))


async def _resolve_contact(
    bx: BitrixClient,
    pairs: set[tuple[str, int]],
    contact_id: int,
) -> None:
    contact = await bx.call("crm.contact.get", {"id": contact_id})
    if contact:
        _add_pair(pairs, "COMPANY", contact.get("COMPANY_ID"))
    async for deal in bx.list(
        "crm.deal.list",
        {"filter": {"CONTACT_ID": contact_id}, "select": ["ID"]},
    ):
        _add_pair(pairs, "DEAL", deal.get("ID"))


async def _resolve_company(
    bx: BitrixClient,
    pairs: set[tuple[str, int]],
    company_id: int,
) -> None:
    async for contact in bx.list(
        "crm.contact.list",
        {"filter": {"COMPANY_ID": company_id}, "select": ["ID"]},
    ):
        _add_pair(pairs, "CONTACT", contact.get("ID"))
    async for deal in bx.list(
        "crm.deal.list",
        {"filter": {"COMPANY_ID": company_id}, "select": ["ID"]},
    ):
        _add_pair(pairs, "DEAL", deal.get("ID"))


async def _crm_entity_pairs(entity_type: str, entity_id: int) -> list[tuple[str, int]]:
    """CRM entities a card's calls may be attached to, resolved across links.

    A call links to exactly **one** CRM entity — usually the **contact**, not
    the deal. So whichever card the user pastes is cross-resolved through Bitrix
    so the same calls surface either way: a deal → its contact(s)/company; a
    contact/company → its deals (and the contact's company / the company's
    contacts). Best-effort: a Bitrix failure degrades to the pasted entity
    alone, which is still a direct hit for contact/lead cards (calls link to the
    entity itself there).
    """
    canonical = CRM_ENTITY_TYPES[entity_type]
    pairs: set[tuple[str, int]] = {(canonical, entity_id)}
    # LEAD: calls link to the lead itself; nothing to cross-resolve.
    resolvers = {
        "DEAL": _resolve_deal,
        "CONTACT": _resolve_contact,
        "COMPANY": _resolve_company,
    }
    resolver = resolvers.get(canonical)
    if resolver is None:
        return list(pairs)
    try:
        async with BitrixClient() as bx:
            await resolver(bx, pairs, entity_id)
    except BitrixError as exc:
        logger.warning(
            "CRM {type}:{id} entity resolution failed: {e}",
            type=canonical,
            id=entity_id,
            e=exc,
        )
    return list(pairs)


def _can_view_call_row(
    identity: CompanionIdentity,
    manager_bitrix_user_id: int | None,
    department_bitrix_id: int | None,
) -> bool:
    """Whether the caller may see a deal call row (mirrors ``ensure_can_view_manager``).

    Filtered, not raised: one CRM card can hold several managers' calls, so the
    feed is trimmed to the visible subset — a manager sees only their own, a
    scoped head only their department's, the global head everything.
    """
    if identity.role is CompanionRole.HEAD:
        if identity.department_id is None:
            return True
        return department_bitrix_id == identity.department_id
    return (
        manager_bitrix_user_id is not None
        and manager_bitrix_user_id == identity.bitrix_user_id
    )


async def get_crm_entity_calls(
    session: AsyncSession,
    entity_type: str,
    entity_id: int,
    identity: CompanionIdentity,
) -> list[CallFeedItem]:
    """Scored calls attached to a Bitrix CRM card (deal/contact/company/lead).

    ``entity_type`` is the card's path segment (``deal``/``contact``/…). The
    card is cross-resolved through Bitrix (see ``_crm_entity_pairs``) and calls
    are matched on any of the linked CRM entities. Rows the caller may not see
    are filtered out, so an unrelated or out-of-scope card returns an empty list
    rather than leaking another manager's calls.
    """
    pairs = await _crm_entity_pairs(entity_type, entity_id)
    conditions: list[str] = []
    params: dict[str, Any] = {}
    for i, (pair_type, pair_id) in enumerate(pairs):
        conditions.append(f"(crm_entity_type = :t{i} AND crm_entity_id = :i{i})")
        params[f"t{i}"] = pair_type
        params[f"i{i}"] = pair_id
    rows = (
        await session.execute(
            # ``conditions`` are code-generated placeholder pairs; all values are
            # bound via :params — no user text reaches the SQL string.
            text(
                "SELECT call_id, bitrix_call_id, started_at, percent, zone, "  # noqa: S608
                "target_status, sentiment_customer, red_flags, call_type, "
                "is_qualification_call, summary, crm_entity_type, crm_entity_id, "
                "manager_bitrix_user_id, department_bitrix_id "
                "FROM call_scores_latest "
                f"WHERE {' OR '.join(conditions)} "
                "ORDER BY started_at DESC NULLS LAST",
            ),
            params,
        )
    ).all()
    rows = await _apply_score_overrides(session, rows)
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
            bitrix_url=crm_card_url(r.crm_entity_type, r.crm_entity_id),
        )
        for r in rows
        if _can_view_call_row(
            identity,
            r.manager_bitrix_user_id,
            r.department_bitrix_id,
        )
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

    transcript_row = (
        await session.execute(
            text("SELECT segments, full_text FROM transcripts WHERE call_id = :cid"),
            {"cid": call_id},
        )
    ).first()
    transcript = (
        _transcript_blocks(transcript_row.segments, transcript_row.full_text)
        if transcript_row is not None
        else []
    )

    # Original LLM percent, captured before any head override is folded in so the
    # appeal card can show "было X% → стало Y%".
    original_percent = float(row.percent) if row.percent is not None else None
    row = _with_overrides([row], await _score_overrides(session, [row.call_id]))[0]
    appeal = await get_latest_appeal_for_call(session, call_id)
    appeal_view = (
        _appeal_view(
            appeal,
            manager_name=row.manager_name,
            started_at=row.started_at,
            original_percent=original_percent,
        )
        if appeal is not None
        else None
    )

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
        bitrix_url=crm_card_url(row.crm_entity_type, row.crm_entity_id),
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
        transcript=transcript,
        appeal=appeal_view,
    )


# --- Appeals (апелляции) -----------------------------------------------------
# A manager files an appeal against a call's ОКК score; the department head
# reviews it and may record a corrected percent (see ``_score_overrides``).
# Writes only AtamuraOKK's own ``appeals`` table — never Bitrix/pipeline state.


def _appeal_view(
    appeal: Appeal,
    *,
    manager_name: str | None = None,
    started_at: datetime | None = None,
    original_percent: float | None = None,
) -> AppealView:
    """Project an ``Appeal`` row into its DTO, with optional call context."""
    override = (
        float(appeal.override_percent)
        if appeal.override_percent is not None
        else None
    )
    return AppealView(
        id=appeal.id,
        call_id=appeal.call_id,
        manager_bitrix_user_id=appeal.manager_bitrix_user_id,
        created_by_bitrix_user_id=appeal.created_by_bitrix_user_id,
        department_id=appeal.department_bitrix_id,
        reason=appeal.reason,
        status=appeal.status,
        override_percent=override,
        override_okk_5=okk.okk_5(override),
        head_note=appeal.head_note,
        reviewed_by_bitrix_user_id=appeal.reviewed_by_bitrix_user_id,
        reviewed_at=appeal.reviewed_at,
        created_at=appeal.created_at,
        manager_name=manager_name,
        started_at=started_at,
        original_percent=original_percent,
    )


async def get_latest_appeal_for_call(
    session: AsyncSession,
    call_id: int,
) -> Appeal | None:
    """The most recent appeal on a call (any status), or None."""
    return await session.scalar(
        select(Appeal)
        .where(Appeal.call_id == call_id)
        .order_by(Appeal.created_at.desc(), Appeal.id.desc()),
    )


async def get_open_appeal_for_call(
    session: AsyncSession,
    call_id: int,
) -> Appeal | None:
    """A still-pending appeal on a call, or None — guards against duplicates."""
    return await session.scalar(
        select(Appeal).where(
            Appeal.call_id == call_id,
            Appeal.status == APPEAL_PENDING,
        ),
    )


async def get_call_score_context(
    session: AsyncSession,
    call_id: int,
) -> Any | None:
    """Scoring context for a call (manager/department/started_at/percent), or None.

    Sources the same read view the rest of the API uses, so a call only counts
    as appealable once it actually has a score there.
    """
    return (
        await session.execute(
            text(
                "SELECT manager_bitrix_user_id, department_bitrix_id, "
                "manager_name, started_at, percent "
                "FROM call_scores_latest WHERE call_id = :cid",
            ),
            {"cid": call_id},
        )
    ).first()


async def create_appeal(
    session: AsyncSession,
    *,
    call_id: int,
    manager_bitrix_user_id: int,
    created_by_bitrix_user_id: int,
    department_bitrix_id: int | None,
    reason: str | None,
) -> Appeal:
    """Persist a new pending appeal and return it."""
    appeal = Appeal(
        call_id=call_id,
        manager_bitrix_user_id=manager_bitrix_user_id,
        created_by_bitrix_user_id=created_by_bitrix_user_id,
        department_bitrix_id=department_bitrix_id,
        reason=reason,
        status=APPEAL_PENDING,
    )
    session.add(appeal)
    await session.flush()
    await session.refresh(appeal)
    return appeal


async def _appeal_context(
    session: AsyncSession,
    call_ids: Sequence[int],
) -> dict[int, Any]:
    """``call_id → row(manager_name, started_at, percent)`` for the review list."""
    ids = sorted(set(call_ids))
    if not ids:
        return {}
    rows = (
        await session.execute(
            text(
                "SELECT call_id, manager_name, started_at, percent "
                "FROM call_scores_latest WHERE call_id = ANY(:ids)",
            ),
            {"ids": ids},
        )
    ).all()
    return {r.call_id: r for r in rows}


async def view_for_appeal(session: AsyncSession, appeal: Appeal) -> AppealView:
    """One appeal as its DTO, enriched with the appealed call's context."""
    ctx = (await _appeal_context(session, [appeal.call_id])).get(appeal.call_id)
    return _appeal_view(
        appeal,
        manager_name=getattr(ctx, "manager_name", None),
        started_at=getattr(ctx, "started_at", None),
        original_percent=(
            float(ctx.percent)
            if ctx is not None and ctx.percent is not None
            else None
        ),
    )


async def list_appeals(
    session: AsyncSession,
    *,
    department_bitrix_id: int | None = None,
    manager_bitrix_user_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[AppealView]:
    """Appeals matching the given scope, newest first, with call context.

    A global head passes no scope; an office РОП passes their
    ``department_bitrix_id``; a manager passes their ``manager_bitrix_user_id``.
    """
    query = select(Appeal).order_by(Appeal.created_at.desc(), Appeal.id.desc())
    if department_bitrix_id is not None:
        query = query.where(Appeal.department_bitrix_id == department_bitrix_id)
    if manager_bitrix_user_id is not None:
        query = query.where(
            Appeal.manager_bitrix_user_id == manager_bitrix_user_id,
        )
    if status is not None:
        query = query.where(Appeal.status == status)
    appeals = list((await session.scalars(query.limit(limit))).all())
    context = await _appeal_context(session, [a.call_id for a in appeals])
    return [
        _appeal_view(
            a,
            manager_name=getattr(context.get(a.call_id), "manager_name", None),
            started_at=getattr(context.get(a.call_id), "started_at", None),
            original_percent=(
                float(ctx.percent)
                if (ctx := context.get(a.call_id)) is not None
                and ctx.percent is not None
                else None
            ),
        )
        for a in appeals
    ]


async def get_appeal(session: AsyncSession, appeal_id: int) -> Appeal | None:
    """Fetch one appeal by its internal id, or None."""
    return await session.get(Appeal, appeal_id)


async def review_appeal(
    session: AsyncSession,
    appeal: Appeal,
    *,
    status: str,
    override_percent: float | None,
    note: str | None,
    reviewed_by_bitrix_user_id: int | None,
) -> Appeal:
    """Record a head's verdict. An override only sticks on an accepted appeal."""
    appeal.status = status
    appeal.override_percent = (
        override_percent if status == APPEAL_ACCEPTED else None
    )
    appeal.head_note = note
    appeal.reviewed_by_bitrix_user_id = reviewed_by_bitrix_user_id
    appeal.reviewed_at = datetime.now(UTC)
    await session.flush()
    await session.refresh(appeal)
    return appeal


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


async def _visits_by_tm(start: datetime, end: datetime) -> dict[int, int]:
    """Conversions to «Фактический визит» per TM for the period, from Bitrix.

    One ``crm.stagehistory.list`` pull (shared/cached with the /day view) keyed
    by TM user id. Degrades to an empty mapping when the webhook is unset or
    Bitrix is unreachable, so the team view simply omits the visit counts
    instead of failing.
    """
    try:
        async with BitrixClient() as bx:
            return await day.conducted_meetings_by_tm(bx, start, end)
    except BitrixError:
        return {}


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
                "SELECT call_id, manager_bitrix_user_id, manager_name, percent, "
                "zone, is_qualification_call FROM call_scores_latest "
                "WHERE department_id = :dept "
                "AND started_at >= :start AND started_at < :end",
            ),
            {"dept": department.id, "start": start, "end": end},
        )
    ).all()
    rows = await _apply_score_overrides(session, rows)

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

    # «Фактический визит» conversions per manager — TM department only (the funnel
    # does not apply to meeting offices). Empty when Bitrix is unavailable.
    is_tm = department.bitrix_id == settings.companion_tm_department_id
    visits = await _visits_by_tm(start, end) if is_tm else {}
    have_visits = is_tm and bool(visits)

    def _money_for(uid: int) -> MoneyAxis:
        if not have_visits:
            return MoneyAxis()
        return MoneyAxis(status="live", meetings=visits.get(uid, 0))

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
                money=_money_for(uid),
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
            money=(
                MoneyAxis(
                    status="live",
                    meetings=sum(
                        visits.get(c.manager.bitrix_user_id, 0) for c in roster
                    ),
                )
                if have_visits
                else MoneyAxis()
            ),
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
