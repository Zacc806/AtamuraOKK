"""Audit freshly closed-lost deals and persist a verdict per deal.

Incremental like ingestion: a ``IngestState`` cursor (``audit_closed_deals``) tracks
the CLOSEDATE watermark. Each pass fetches deals closed-lost since the cursor
(``STAGE_SEMANTIC_ID='F'`` in the TM category), joins each to the client's call
transcript(s) we already hold, LLM-judges the stated close reason against the call,
and upserts an :class:`AuditVerdict` (idempotent on ``bitrix_deal_id``). Deals
without a transcript are skipped; deals already judged (a non-error verdict) are not
re-judged; the cursor only advances past deals that are definitively done, so an
``error`` (e.g. the API out of credits) is retried on the next pass.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from AtamuraOKK.audit.judge import build_judge_client, judge_one
from AtamuraOKK.db.models.audit_verdict import AuditVerdict
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.ingest_state import IngestState
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.ingestion.managers import ensure_managers
from AtamuraOKK.settings import settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from AtamuraOKK.bitrix import BitrixClient

AUDIT_CURSOR_KEY = "audit_closed_deals"
_UNSPECIFIED_REASON = "Не указана"
_CONCURRENCY = 6


@dataclass
class AuditStats:
    """Summary of one audit pass."""

    scanned: int = 0
    judged: int = 0
    no_transcript: int = 0
    already_done: int = 0
    cursor: str | None = None
    verdicts: dict[str, int] = field(default_factory=dict)


async def _get_cursor(session: AsyncSession) -> str | None:
    state = await session.scalar(
        select(IngestState).where(IngestState.key == AUDIT_CURSOR_KEY),
    )
    return state.last_cursor if state else None


async def _set_cursor(session: AsyncSession, value: str) -> None:
    state = await session.scalar(
        select(IngestState).where(IngestState.key == AUDIT_CURSOR_KEY),
    )
    if state:
        state.last_cursor = value
    else:
        session.add(IngestState(key=AUDIT_CURSOR_KEY, last_cursor=value))


async def reason_enum_labels(bx: BitrixClient, field_name: str) -> dict[str, str]:
    """``{enum id: label}`` for the deal reason field (mirrors analytics.py)."""
    fields = await bx.call("crm.deal.fields")
    meta = fields.get(field_name) if isinstance(fields, dict) else None
    labels: dict[str, str] = {}
    for item in (meta or {}).get("items") or []:
        fid, value = item.get("ID"), item.get("VALUE")
        if fid is not None and value is not None:
            labels[str(fid)] = str(value)
    return labels


def reason_ids(raw: Any) -> list[str]:
    """Non-empty enum value ids from a deal's reason field (single- or multi-select)."""
    values = raw if isinstance(raw, list) else [raw]
    return [str(v) for v in values if v not in (None, "", 0, "0")]


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


async def _transcripts_for_client(
    session: AsyncSession, client_key: str
) -> tuple[list[int], str]:
    """Concatenated, call-labeled transcript for one client key (empty if none)."""
    rows = (
        await session.execute(
            select(Call.id, Call.started_at, Transcript.full_text)
            .join(Transcript, Transcript.call_id == Call.id)
            .where(Call.client_key == client_key)
            .order_by(Call.started_at),
        )
    ).all()
    if not rows:
        return [], ""
    call_ids = [r.id for r in rows]
    blocks = [f"=== звонок {r.id} @ {r.started_at} ===\n{r.full_text}" for r in rows]
    return call_ids, "\n\n".join(blocks)


async def _upsert_verdict(
    session: AsyncSession,
    deal: dict[str, Any],
    reason_label: str,
    reason_id: str | None,
    call_ids: list[int],
    closed_at: datetime | None,
    manager_id: int | None,
    verdict: dict[str, Any],
    model: str,
) -> None:
    """Idempotent upsert of one deal's verdict (mirrors scoring _persist_score)."""
    assigned = deal.get("ASSIGNED_BY_ID")
    contact = deal.get("CONTACT_ID")
    values = {
        "bitrix_deal_id": int(deal["ID"]),
        "deal_title": deal.get("TITLE"),
        "manager_id": manager_id,
        "assigned_by_id": int(assigned) if assigned else None,
        "client_key": f"CONTACT:{contact}" if contact else None,
        "close_reason": reason_label,
        "reason_id": reason_id,
        "verdict": str(verdict.get("verdict") or "error"),
        "confidence": verdict.get("confidence"),
        "justification": verdict.get("justification"),
        "evidence_quote": verdict.get("evidence_quote"),
        "call_ids": call_ids,
        "closed_at": closed_at,
        "model": model,
    }
    stmt = insert(AuditVerdict).values(**values)
    # notified_at is deliberately excluded so a re-audit never re-notifies.
    update_cols = {
        c: stmt.excluded[c] for c in values if c not in ("bitrix_deal_id",)
    }
    update_cols["audited_at"] = stmt.excluded.audited_at
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_audit_verdicts_deal",
            set_=update_cols,
        ),
    )


async def _resolve_targets(
    session: AsyncSession,
    deals: list[dict[str, Any]],
    done_ids: set[int],
    labels: dict[str, str],
    field_name: str,
    stats: AuditStats,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve each deal's transcript; return (ordered entries, deals to judge)."""
    entries: list[dict[str, Any]] = []
    to_judge: list[dict[str, Any]] = []
    for d in deals:
        stats.scanned += 1
        closedate = str(d.get("CLOSEDATE") or "")
        if int(d["ID"]) in done_ids:
            stats.already_done += 1
            entries.append({"cd": closedate, "done": True})
            continue
        contact_id = d.get("CONTACT_ID")
        client_key = (
            f"CONTACT:{contact_id}" if contact_id not in (None, "", 0, "0") else None
        )
        call_ids, transcript = (
            await _transcripts_for_client(session, client_key)
            if client_key
            else ([], "")
        )
        if not call_ids:
            stats.no_transcript += 1
            entries.append({"cd": closedate, "done": True})
            continue
        ids = reason_ids(d.get(field_name))
        reason_id = ids[0] if ids else None
        label = labels.get(reason_id, reason_id) if reason_id else _UNSPECIFIED_REASON
        entries.append({"cd": closedate, "done": None, "idx": len(to_judge)})
        to_judge.append(
            {
                "deal": d,
                "reason_label": label,
                "reason_id": reason_id,
                "call_ids": call_ids,
                "transcript": transcript,
                "closed_at": _parse_dt(d.get("CLOSEDATE")),
            }
        )
    return entries, to_judge


async def _judge_and_persist(
    session: AsyncSession,
    bx: BitrixClient,
    entries: list[dict[str, Any]],
    to_judge: list[dict[str, Any]],
    stats: AuditStats,
) -> None:
    """Judge all pending deals concurrently and upsert their verdicts."""
    assigned = {
        int(x["deal"]["ASSIGNED_BY_ID"])
        for x in to_judge
        if x["deal"].get("ASSIGNED_BY_ID")
    }
    mgr_map = await ensure_managers(session, assigned, bx) if assigned else {}
    judge_client = build_judge_client()
    model = settings.anthropic_scoring_model
    sem = asyncio.Semaphore(_CONCURRENCY)
    results = await asyncio.gather(
        *(
            judge_one(
                judge_client,
                transcript=x["transcript"],
                close_reason=x["reason_label"],
                model=model,
                sem=sem,
            )
            for x in to_judge
        )
    )
    for e in entries:
        if e.get("done") is not None:
            continue
        x = to_judge[e["idx"]]
        verdict = results[e["idx"]]
        assigned_id = x["deal"].get("ASSIGNED_BY_ID")
        manager_id = mgr_map.get(int(assigned_id)) if assigned_id else None
        await _upsert_verdict(
            session,
            x["deal"],
            x["reason_label"],
            x["reason_id"],
            x["call_ids"],
            x["closed_at"],
            manager_id,
            verdict,
            model,
        )
        name = str(verdict.get("verdict"))
        e["done"] = name != "error"
        stats.judged += 1
        stats.verdicts[name] = stats.verdicts.get(name, 0) + 1


async def run_audit(
    session: AsyncSession,
    bx: BitrixClient,
    *,
    limit: int | None = None,
) -> AuditStats:
    """One incremental audit pass over freshly closed-lost deals."""
    stats = AuditStats()
    cursor = await _get_cursor(session)
    stats.cursor = cursor
    field_name = settings.companion_closed_reason_field
    if not field_name:
        logger.warning("audit: companion_closed_reason_field unset — nothing to do")
        return stats

    filter_: dict[str, Any] = {
        "CATEGORY_ID": settings.companion_tm_category_id,
        "STAGE_SEMANTIC_ID": "F",
    }
    if cursor:
        filter_[">=CLOSEDATE"] = cursor
    deals: list[dict[str, Any]] = []
    async for d in bx.list(
        "crm.deal.list",
        {
            "filter": filter_,
            "select": [
                "ID",
                "TITLE",
                "ASSIGNED_BY_ID",
                "CONTACT_ID",
                "CLOSEDATE",
                field_name,
            ],
            "order": {"CLOSEDATE": "ASC"},
        },
        max_items=limit,
    ):
        deals.append(d)

    if not deals:
        return stats

    # Skip deals already judged (non-error) — cheap re-run + retry of errors only.
    deal_ids = [int(d["ID"]) for d in deals]
    done_ids = set(
        (
            await session.execute(
                select(AuditVerdict.bitrix_deal_id).where(
                    AuditVerdict.bitrix_deal_id.in_(deal_ids),
                    AuditVerdict.verdict != "error",
                ),
            )
        )
        .scalars()
        .all()
    )
    labels = await reason_enum_labels(bx, field_name)

    entries, to_judge = await _resolve_targets(
        session, deals, done_ids, labels, field_name, stats
    )
    if to_judge:
        await _judge_and_persist(session, bx, entries, to_judge, stats)

    # Advance the cursor only across the contiguous leading run of done deals,
    # so an errored deal (and everything after it) is retried next pass.
    new_cursor = cursor
    for e in entries:
        if not e["done"]:
            break
        if e["cd"]:
            new_cursor = e["cd"]
    if new_cursor and new_cursor != cursor:
        await _set_cursor(session, new_cursor)
        stats.cursor = new_cursor

    logger.info(
        "audit pass: scanned={s} judged={j} no_transcript={nt} already_done={ad} "
        "verdicts={v} cursor={c}",
        s=stats.scanned,
        j=stats.judged,
        nt=stats.no_transcript,
        ad=stats.already_done,
        v=stats.verdicts,
        c=stats.cursor,
    )
    return stats
