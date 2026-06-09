"""Incremental Bitrix -> Postgres ingestion (Phase 1).

Pulls answered, recorded calls since the stored cursor; upserts them idempotently
on ``bitrix_call_id``; attributes each to a Manager; marks the *first call per
client*; checks qualification; and flags which calls are ``analyzable`` (first
call AND qualified). Downloading audio is a separate stage (``download.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.bitrix import BitrixClient
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.models.ingest_state import IngestState
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.ingestion.managers import ensure_managers
from AtamuraOKK.ingestion.mapping import parse_started_at, to_call_fields
from AtamuraOKK.ingestion.qualification import QualificationChecker, default_checker
from AtamuraOKK.settings import settings

CURSOR_KEY = "calls"


@dataclass
class IngestStats:
    """Summary of one ingestion run."""

    scanned: int = 0
    upserted: int = 0
    analyzable: int = 0
    skipped: int = 0
    cursor: str | None = None
    skip_reasons: dict[str, int] = field(default_factory=dict)


def _is_answered_recorded(row: dict[str, Any]) -> bool:
    """Keep only answered calls of usable length that carry a recording."""
    if row.get("CALL_FAILED_CODE") != settings.ingest_success_code:
        return False
    if int(row.get("CALL_DURATION") or 0) < settings.ingest_min_duration_sec:
        return False
    return bool(row.get("CALL_RECORD_URL") or row.get("RECORD_FILE_ID"))


async def _get_cursor(session: AsyncSession) -> str | None:
    state = await session.scalar(
        select(IngestState).where(IngestState.key == CURSOR_KEY),
    )
    return state.last_cursor if state else None


async def _set_cursor(session: AsyncSession, value: str) -> None:
    state = await session.scalar(
        select(IngestState).where(IngestState.key == CURSOR_KEY),
    )
    if state:
        state.last_cursor = value
    else:
        session.add(IngestState(key=CURSOR_KEY, last_cursor=value))


def _parse_cursor(cursor: str | None) -> datetime | None:
    """Stored cursor -> aware datetime (older naive cursors are read as UTC)."""
    if not cursor:
        return None
    try:
        dt = datetime.fromisoformat(cursor)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _since_filter(cursor: str | None) -> str:
    """Lower bound for CALL_START_DATE, with overlap so nothing slips the cursor.

    Emitted as ISO-8601 *with offset* so Bitrix interprets the instant
    unambiguously; lexicographic/naive comparison could otherwise skip calls.
    """
    base = _parse_cursor(cursor)
    if base is None:
        base = datetime.now(tz=UTC) - timedelta(days=settings.ingest_initial_days_back)
    else:
        base -= timedelta(minutes=settings.ingest_overlap_minutes)
    return base.isoformat()


async def _upsert_call(session: AsyncSession, fields: dict[str, Any]) -> None:
    """Insert or update a call by ``bitrix_call_id`` (ingestion-owned columns only).

    Never clobbers pipeline-managed columns (status, analyzable, audio_object_key,
    ...) so re-ingesting an already-processed call is safe.
    """
    stmt = insert(Call).values(**fields)
    update_cols = {
        c: stmt.excluded[c]
        for c in (
            "bitrix_row_id",
            "portal_user_id",
            "direction",
            "started_at",
            "duration_sec",
            "phone_number",
            "crm_entity_type",
            "crm_entity_id",
            "crm_activity_id",
            "client_key",
            "recording_url",
            "record_file_id",
        )
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["bitrix_call_id"],
        set_=update_cols,
    )
    await session.execute(stmt)


async def _attribute_managers(
    session: AsyncSession,
    user_ids: set[int],
    bx: BitrixClient,
) -> None:
    uid_to_mid = await ensure_managers(session, user_ids, bx)
    for uid, mid in uid_to_mid.items():
        await session.execute(
            update(Call)
            .where(Call.portal_user_id == uid, Call.manager_id.is_(None))
            .values(manager_id=mid),
        )


async def _recompute_scope(
    session: AsyncSession,
    client_keys: set[str],
    checker: QualificationChecker,
    bx: BitrixClient,
    stats: IngestStats,
) -> None:
    """Mark first-call + qualification + analyzable + status for affected clients."""
    quals = await checker.qualified(client_keys, bx)

    for key in client_keys:
        calls = (
            await session.scalars(
                select(Call)
                .where(Call.client_key == key)
                .order_by(Call.started_at.asc(), Call.bitrix_row_id.asc()),
            )
        ).all()
        if not calls:
            continue
        qualified = quals.get(key)
        for idx, call in enumerate(calls):
            call.is_first_call = idx == 0
            call.client_qualified = qualified
            _apply_scope(call, qualified, stats)


def _apply_scope(
    call: Call,
    qualified: bool | None,
    stats: IngestStats,
) -> None:
    """Set ``analyzable``/``status``/``skip_reason`` for one call.

    Only first calls are candidates; qualification gates them when required.
    Never demotes a call that already moved past NEW.
    """
    if call.status not in (CallStatus.NEW, CallStatus.SKIPPED):
        return  # already in flight / done; leave it

    reason: str | None = None
    # Duration gate first: a sub-threshold call is never a scorable conversation,
    # no matter how it entered the table (legacy rows, requalification, etc.).
    # Defense in depth behind the ingestion-scan filter (_is_answered_recorded).
    if call.duration_sec < settings.ingest_min_duration_sec:
        reason = "too_short"
    elif not call.is_first_call:
        reason = "not_first_call"
    elif settings.ingest_require_qualified and qualified is not True:
        reason = "not_qualified" if qualified is False else "qualification_unknown"

    if reason:
        call.analyzable = False
        call.status = CallStatus.SKIPPED
        call.skip_reason = reason
        stats.skipped += 1
        stats.skip_reasons[reason] = stats.skip_reasons.get(reason, 0) + 1
    else:
        call.analyzable = True
        call.skip_reason = None
        call.status = CallStatus.NEW
        stats.analyzable += 1


async def run_ingestion(
    *,
    limit: int | None = None,
    checker: QualificationChecker | None = None,
) -> IngestStats:
    """Run one incremental ingestion pass; returns a summary."""
    checker = checker or default_checker()
    stats = IngestStats()

    async with session_scope() as session, BitrixClient() as bx:
        cursor = await _get_cursor(session)
        since = _since_filter(cursor)
        logger.info(
            "Ingesting calls since {since} (cursor={cursor})",
            since=since,
            cursor=cursor,
        )

        params = {
            "FILTER": {">=CALL_START_DATE": since},
            "ORDER": {"CALL_START_DATE": "ASC"},
        }
        client_keys: set[str] = set()
        user_ids: set[int] = set()
        max_started: datetime | None = _parse_cursor(cursor)

        async for row in bx.list("voximplant.statistic.get", params, max_items=limit):
            stats.scanned += 1
            if not _is_answered_recorded(row):
                continue
            fields = to_call_fields(row)
            if not fields.get("client_key"):
                continue  # cannot attribute to a client -> out of scope
            await _upsert_call(session, fields)
            stats.upserted += 1
            client_keys.add(fields["client_key"])
            if fields.get("portal_user_id"):
                user_ids.add(int(fields["portal_user_id"]))
            started = parse_started_at(row)
            if started and (max_started is None or started > max_started):
                max_started = started

        await session.flush()
        await _attribute_managers(session, user_ids, bx)
        await _recompute_scope(session, client_keys, checker, bx, stats)

        if max_started:
            cursor_value = max_started.isoformat()
            await _set_cursor(session, cursor_value)
            stats.cursor = cursor_value

    logger.info(
        "Ingestion done: scanned={s} upserted={u} analyzable={a} skipped={k} {r}",
        s=stats.scanned,
        u=stats.upserted,
        a=stats.analyzable,
        k=stats.skipped,
        r=stats.skip_reasons,
    )
    return stats


# Skip reasons that a later qualification refresh can still flip to analyzable.
_REQUALIFIABLE = ("not_qualified", "qualification_unknown")


async def refresh_qualification(
    *,
    limit: int = 1000,
    checker: QualificationChecker | None = None,
) -> IngestStats:
    """Re-check first calls whose client wasn't qualified yet, and promote them.

    Clients qualify *after* their first call (as the manager works the deal), so
    this runs periodically to move newly-qualified first calls SKIPPED -> NEW.
    """
    checker = checker or default_checker()
    stats = IngestStats()

    async with session_scope() as session, BitrixClient() as bx:
        calls = (
            await session.scalars(
                select(Call)
                .where(
                    Call.is_first_call.is_(True),
                    Call.status == CallStatus.SKIPPED,
                    Call.skip_reason.in_(_REQUALIFIABLE),
                )
                .limit(limit),
            )
        ).all()
        client_keys = {c.client_key for c in calls if c.client_key}
        quals = await checker.qualified(client_keys, bx)
        for call in calls:
            if not call.client_key:
                continue
            qualified = quals.get(call.client_key)
            call.client_qualified = qualified
            _apply_scope(call, qualified, stats)

    logger.info(
        "Requalification done: checked={c} promoted={a} still_skipped={k}",
        c=len(calls),
        a=stats.analyzable,
        k=stats.skipped,
    )
    return stats
