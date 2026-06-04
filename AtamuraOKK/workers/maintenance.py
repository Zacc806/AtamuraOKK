"""Maintenance jobs: daily summary, requeue of FAILED calls, audio retention."""

from __future__ import annotations

import time
from pathlib import Path

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from AtamuraOKK.db.dao.call_dao import CallDAO
from AtamuraOKK.settings import settings

_SECONDS_PER_DAY = 86400


async def run_daily_summary(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
    """Log a pipeline-health snapshot (calls grouped by status)."""
    async with factory() as session:
        counts = await CallDAO(session).count_by_status()
    logger.info("daily summary — calls by status: {counts}", counts=counts)
    return counts


async def run_requeue_failed(factory: async_sessionmaker[AsyncSession]) -> int:
    """Re-queue retryable FAILED calls. Returns how many were re-queued."""
    async with factory() as session:
        requeued = await CallDAO(session).requeue_failed(
            max_attempts=settings.max_call_attempts,
            limit=settings.requeue_batch_size,
        )
        await session.commit()
    if requeued:
        logger.info("requeued {n} failed calls", n=requeued)
    return requeued


def run_cleanup_audio(*, now: float | None = None) -> int:
    """Delete downloaded audio older than the retention window.

    Transcripts and scores are kept forever; only the heavy audio is purged.
    :returns: number of files deleted.
    """
    root: Path = settings.audio_dir
    if not root.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - (
        settings.audio_retention_days * _SECONDS_PER_DAY
    )
    deleted = 0
    for path in root.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("retention: cannot delete {p}: {e}", p=path, e=exc)
            else:
                deleted += 1
    if deleted:
        logger.info("retention: deleted {n} old audio files", n=deleted)
    return deleted
