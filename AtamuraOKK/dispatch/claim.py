"""Race-safe work-claiming for the status-driven pipeline.

The pipeline advances each call through ``calls.status``. To let several worker
processes cooperate without double-processing (duplicate downloads, duplicate
*paid* Whisper/LLM calls), a claimant atomically flips a batch of "ready" rows
into an in-flight status in one statement using ``SELECT ... FOR UPDATE SKIP
LOCKED``. Two claimants running concurrently get **disjoint** id sets, and a
claimed row leaves the ready set immediately so it isn't re-claimed.

The in-flight status *is* the claim; ``claimed_at`` exists only so a crashed
worker's claim can be reverted by :func:`reclaim_stale`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import func, select, update

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.settings import settings


@dataclass(frozen=True)
class Stage:
    """One pipeline stage's claim transition and its broker queue name."""

    name: str  # also the arq queue / task label, e.g. "transcribe"
    ready: CallStatus  # rows in this status are ready to claim
    in_flight: CallStatus  # claimed rows are flipped to this status
    stale_seconds: int  # claims older than this are reverted (crash recovery)
    today_only: bool = False  # auto-claim only calls started today (see settings)


def report_today_start() -> datetime:
    """Midnight today in the report timezone, as a tz-aware datetime.

    The cutoff for "today's calls": automatic scoring claims only rows whose
    ``started_at`` is at or after this instant; older calls wait for a manual run.
    """
    tz = ZoneInfo(settings.report_timezone)
    now = datetime.now(tz)
    return datetime(now.year, now.month, now.day, tzinfo=tz)


def _stages() -> tuple[Stage, ...]:
    """Stage table (settings read lazily so tests can override knobs)."""
    return (
        Stage(
            "download",
            CallStatus.NEW,
            CallStatus.DOWNLOADING,
            settings.claim_stale_seconds_download,
        ),
        Stage(
            "transcribe",
            CallStatus.DOWNLOADED,
            CallStatus.TRANSCRIBING,
            settings.claim_stale_seconds_transcribe,
        ),
        Stage(
            "score",
            CallStatus.TRANSCRIBED,
            CallStatus.SCORING,
            settings.claim_stale_seconds_score,
            today_only=True,
        ),
    )


STAGES = _stages()


async def claim_ready(
    src: CallStatus,
    dst: CallStatus,
    limit: int,
    *,
    since: datetime | None = None,
) -> list[int]:
    """Atomically claim up to ``limit`` analyzable rows from ``src`` into ``dst``.

    Returns the ids of the rows this caller now owns. ``FOR UPDATE SKIP LOCKED``
    guarantees concurrent callers receive disjoint sets. When ``since`` is given,
    only rows whose ``started_at`` is at or after it are claimed (used to restrict
    automatic scoring to today's calls).
    """
    conditions = [Call.status == src, Call.analyzable.is_(True)]
    if since is not None:
        conditions.append(Call.started_at >= since)
    candidates = (
        select(Call.id)
        .where(*conditions)
        .order_by(Call.started_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    async with session_scope() as session:
        claimed = (
            await session.scalars(
                update(Call)
                .where(Call.id.in_(candidates.scalar_subquery()))
                .values(status=dst, claimed_at=func.now())
                .returning(Call.id),
            )
        ).all()
    return list(claimed)


async def reclaim_stale(
    in_flight: CallStatus,
    src: CallStatus,
    ttl_seconds: int,
) -> int:
    """Revert claims stuck in ``in_flight`` past ``ttl_seconds`` back to ``src``.

    A worker that crashed mid-call leaves a row in an in-flight status forever;
    this reverts it so the next dispatch pass re-claims it. Returns the count
    reclaimed.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=ttl_seconds)
    async with session_scope() as session:
        reclaimed = (
            await session.scalars(
                update(Call)
                .where(Call.status == in_flight, Call.claimed_at < cutoff)
                .values(status=src, claimed_at=None)
                .returning(Call.id),
            )
        ).all()
    if reclaimed:
        logger.warning(
            "Reclaimed {n} stale {st} claim(s) -> {src}",
            n=len(reclaimed),
            st=in_flight.value,
            src=src.value,
        )
    return len(reclaimed)


async def reclaim_all_stale() -> int:
    """Reclaim stale claims across every stage; returns total reclaimed."""
    total = 0
    for stage in STAGES:
        total += await reclaim_stale(stage.in_flight, stage.ready, stage.stale_seconds)
    return total
