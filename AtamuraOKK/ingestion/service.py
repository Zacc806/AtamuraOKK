"""Incremental Bitrix -> Postgres ingestion (Phase 1).

Pulls answered, recorded calls since the stored cursor; upserts them idempotently
on ``bitrix_call_id``; attributes each to a Manager; resolves each client's
qualification moment; and flags which calls are ``analyzable``: **every call of
usable length until the client enters «Лид квалифицирован»** (calls after that
moment are visit logistics, not sales conversations). Unknown qualification
(phone-only clients) counts as not-yet-qualified, i.e. in scope. Downloading
audio is a separate stage (``download.py``).
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
from AtamuraOKK.ingestion.qualification import (
    UNKNOWN_QUALIFICATION,
    Qualification,
    QualificationChecker,
    default_checker,
)
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
        qual = quals.get(key) or UNKNOWN_QUALIFICATION
        for idx, call in enumerate(calls):
            call.is_first_call = idx == 0  # kept as a data point; no longer gates
            call.client_qualified = qual.qualified
            call.client_qualified_at = qual.at
            _apply_scope(call, qual, stats)


# Skip reasons written by the pre-2026-06-12 rule (first call AND qualified).
# The until-qualified rule was applied **forward-only** (operator decision):
# these verdicts are frozen — never recomputed, never promoted.
_LEGACY_SKIP_REASONS = ("not_first_call", "not_qualified", "qualification_unknown")


def _apply_scope(
    call: Call,
    qual: Qualification,
    stats: IngestStats,
) -> None:
    """Set ``analyzable``/``status``/``skip_reason`` for one call.

    The rule: every call of usable length is analyzable **until the client
    qualifies** (``started_at`` past the qualification moment -> skipped).
    Never qualified / unknown -> in scope. Never demotes a call that already
    moved past NEW, and never reopens a legacy-rule verdict (forward-only).
    """
    if call.status not in (CallStatus.NEW, CallStatus.SKIPPED):
        return  # already in flight / done; leave it
    if call.status is CallStatus.SKIPPED and call.skip_reason in _LEGACY_SKIP_REASONS:
        return  # frozen by the old rule

    reason: str | None = None
    # Duration gate first: a sub-threshold call is never a scorable conversation,
    # no matter how it entered the table (legacy rows, requalification, etc.).
    # Defense in depth behind the ingestion-scan filter (_is_answered_recorded).
    if call.duration_sec < settings.ingest_min_duration_sec:
        reason = "too_short"
    elif (
        settings.ingest_until_qualified
        and qual.at is not None
        and call.started_at is not None
        and call.started_at > qual.at
    ):
        reason = "after_qualification"

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


# Window for the late-qualification re-check. A client qualifying later than
# this after a still-unprocessed call is vanishingly rare, and the only cost of
# missing one is scoring a post-qualification call (never losing one). The
# window also keeps old stuck NEW rows from hammering Bitrix every pass.
_REQUALIFY_WINDOW_DAYS = 7


async def refresh_qualification(
    *,
    limit: int = 1000,
    checker: QualificationChecker | None = None,
) -> IngestStats:
    """Late-qualification sync: skip post-qualification calls before they score.

    Under the until-qualified rule nothing waits on qualification (a
    not-yet-qualified client's calls are analyzable immediately). What can
    change later is the qualification *moment* arriving after a call was
    ingested: this pass re-checks clients of recent unclaimed NEW calls with no
    known qualification yet, stamping ``client_qualified_at`` and skipping any
    call that turns out to start after it.
    """
    checker = checker or default_checker()
    stats = IngestStats()

    async with session_scope() as session, BitrixClient() as bx:
        horizon = datetime.now(tz=UTC) - timedelta(days=_REQUALIFY_WINDOW_DAYS)
        calls = (
            await session.scalars(
                select(Call)
                .where(
                    Call.status == CallStatus.NEW,
                    Call.claimed_at.is_(None),
                    Call.client_qualified_at.is_(None),
                    Call.started_at >= horizon,
                )
                .limit(limit),
            )
        ).all()
        client_keys = {c.client_key for c in calls if c.client_key}
        quals = await checker.qualified(client_keys, bx)
        for call in calls:
            if not call.client_key:
                continue
            qual = quals.get(call.client_key) or UNKNOWN_QUALIFICATION
            call.client_qualified = qual.qualified
            call.client_qualified_at = qual.at
            _apply_scope(call, qual, stats)

    logger.info(
        "Requalification done: checked={c} kept={a} skipped={k} {r}",
        c=len(calls),
        a=stats.analyzable,
        k=stats.skipped,
        r=stats.skip_reasons,
    )
    return stats
