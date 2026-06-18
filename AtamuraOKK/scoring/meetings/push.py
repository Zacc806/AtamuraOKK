"""Mirror SCORED meetings into the shared Postgres ``meetings`` table.

This is the one place the meeting pipeline crosses into the call pipeline's
Postgres: scored results are published there so the companion cabinet (and
Metabase) can show ОП meetings next to ТМ calls. SQLite stays the working
state — a row is pushed once (``pushed_at``) and the upsert is idempotent on
``bitrix_file_id``, so a re-push after a crash just refreshes the same row.

Each meeting is attributed to the Bitrix user who uploaded the recording
(Disk ``CREATED_BY``): a ``managers`` row is get-or-created exactly like call
ingestion does (enriched via ``user.get`` when the scope allows, placeholder
otherwise). Postgres being down is non-fatal — rows stay unpushed and are
retried on the next pass.

All call-pipeline imports are lazy, so every other meetings command still runs
without the Postgres stack installed/reachable.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from loguru import logger

from AtamuraOKK.scoring.meetings.config import config
from AtamuraOKK.scoring.meetings.store import MeetingStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class PushStats:
    """Summary of one Postgres-push pass."""

    attempted: int = 0
    pushed: int = 0
    failed: int = 0


def _parse_dt(value: str | None) -> datetime | None:
    """ISO string → aware datetime (naive values get the worker timezone)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(config.meetings_worker_timezone))
    return dt


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    """Postgres column values for one SCORED SQLite row."""
    score: dict[str, Any] = json.loads(row["score_json"]) if row["score_json"] else {}
    red_flags = score.get("red_flags")
    return {
        "bitrix_file_id": int(row["file_id"]),
        "name": row["name"],
        "folder_path": row["folder_path"],
        "source": config.meetings_source,
        "uploaded_by_bitrix_id": row["created_by"],
        "meeting_at": _parse_dt(row["meeting_at"]) or _parse_dt(row["created_at"]),
        "duration_sec": row["duration_sec"],
        "language": score.get("language") or row["language"],
        "rubric_version": score.get("rubric_version"),
        "score_pct": row["score_pct"],
        "passed": bool(row["passed"]) if row["passed"] is not None else None,
        "call_type": score.get("call_type"),
        "manager_tone": score.get("manager_tone"),
        "needs_human_review": bool(score.get("needs_human_review", False)),
        "summary": score.get("summary"),
        "red_flags": red_flags if isinstance(red_flags, list) else [],
        "score": score,
    }


async def _resolve_managers(
    session: AsyncSession,
    user_ids: set[int],
) -> dict[int, int]:
    """{bitrix_user_id: managers.id}, get-or-creating rows like call ingestion.

    Tries the full ``ensure_managers`` path (creates + enriches via Bitrix
    ``user.get``); if Bitrix is unreachable falls back to bare placeholder
    rows so attribution never blocks the push.
    """
    if not user_ids:
        return {}
    from sqlalchemy import select  # noqa: PLC0415

    from AtamuraOKK.db.models.manager import Manager  # noqa: PLC0415

    try:
        from AtamuraOKK.bitrix import BitrixClient  # noqa: PLC0415
        from AtamuraOKK.ingestion.managers import ensure_managers  # noqa: PLC0415

        async with BitrixClient() as bx:
            return await ensure_managers(session, user_ids, bx)
    except Exception as exc:
        logger.warning(
            "Meeting push: manager enrichment unavailable ({e}); "
            "creating placeholder rows",
            e=exc,
        )
    rows = (
        await session.scalars(
            select(Manager).where(Manager.bitrix_user_id.in_(user_ids)),
        )
    ).all()
    by_uid = {m.bitrix_user_id: m for m in rows}
    for uid in user_ids - set(by_uid):
        manager = Manager(bitrix_user_id=uid)
        session.add(manager)
        by_uid[uid] = manager
    await session.flush()
    return {uid: m.id for uid, m in by_uid.items()}


async def _push_batch(
    session: AsyncSession,
    payloads: list[dict[str, Any]],
) -> list[int]:
    """Upsert payloads into ``meetings``; returns the pushed Bitrix file ids."""
    from sqlalchemy import String, func  # noqa: PLC0415
    from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: PLC0415

    from AtamuraOKK.db.models.meeting import Meeting  # noqa: PLC0415

    # A chatty LLM can return e.g. a call_type far longer than its VARCHAR;
    # truncate every string column to its width so the upsert can never fail.
    str_limits = {
        c.name: c.type.length
        for c in Meeting.__table__.columns
        if isinstance(c.type, String) and c.type.length is not None
    }

    uids = {
        int(p["uploaded_by_bitrix_id"])
        for p in payloads
        if p["uploaded_by_bitrix_id"] is not None
    }
    manager_ids = await _resolve_managers(session, uids)

    pushed: list[int] = []
    for payload in payloads:
        uid = payload["uploaded_by_bitrix_id"]
        values = {
            **payload,
            "manager_id": manager_ids.get(int(uid)) if uid is not None else None,
        }
        for col, limit in str_limits.items():
            text = values.get(col)
            if isinstance(text, str) and len(text) > limit:
                logger.warning(
                    "Meeting push: clamped {col} ({n}>{limit}) for file {fid}",
                    col=col,
                    n=len(text),
                    limit=limit,
                    fid=payload["bitrix_file_id"],
                )
                values[col] = text[:limit]
        stmt = pg_insert(Meeting).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Meeting.bitrix_file_id],
            set_={
                **{k: v for k, v in values.items() if k != "bitrix_file_id"},
                "updated_at": func.now(),
            },
        )
        await session.execute(stmt)
        pushed.append(payload["bitrix_file_id"])
    await session.flush()
    return pushed


async def push_pending(
    *,
    limit: int | None = None,
    store: MeetingStore | None = None,
    session: AsyncSession | None = None,
) -> PushStats:
    """Publish SCORED-but-unpushed recordings to Postgres → set ``pushed_at``.

    With ``session`` given (tests) the caller owns the transaction; otherwise a
    ``session_scope()`` commits the batch before SQLite is marked, so a row is
    flagged pushed only after Postgres durably has it.
    """
    stats = PushStats()
    limit = limit if limit is not None else config.meetings_batch_limit
    own_store = store is None
    store = store or MeetingStore()
    try:
        rows = store.claim_unpushed(limit)
        if not rows:
            return stats

        payloads: list[dict[str, Any]] = []
        for row in rows:
            stats.attempted += 1
            try:
                payloads.append(_payload(row))
            except Exception as exc:
                stats.failed += 1
                logger.warning(
                    "Meeting push: bad score payload for {id}: {e}",
                    id=row["file_id"],
                    e=exc,
                )
        if not payloads:
            return stats

        try:
            if session is not None:
                pushed_ids = await _push_batch(session, payloads)
            else:
                from AtamuraOKK.db.session import session_scope  # noqa: PLC0415

                async with session_scope() as scoped:
                    pushed_ids = await _push_batch(scoped, payloads)
        except Exception:
            stats.failed += len(payloads)
            logger.exception(
                "Meeting push: Postgres write failed; {n} rows stay unpushed "
                "and will be retried next pass",
                n=len(payloads),
            )
            return stats

        for file_id in pushed_ids:
            store.mark_pushed(file_id)
            stats.pushed += 1
    finally:
        if own_store:
            store.close()

    logger.info(
        "Meeting push: attempted={a} pushed={p} failed={f}",
        a=stats.attempted,
        p=stats.pushed,
        f=stats.failed,
    )
    return stats
