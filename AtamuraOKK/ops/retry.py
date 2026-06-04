"""Requeue FAILED calls for another attempt; surface the dead-letter queue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy import select

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.settings import settings


@dataclass
class RetryStats:
    """Result of a requeue pass."""

    requeued: int = 0
    to_new: int = 0
    to_downloaded: int = 0
    to_transcribed: int = 0
    dead_lettered: int = 0


async def requeue_failed(*, limit: int = 500) -> RetryStats:
    """Reset FAILED calls (under the retry cap) to their prior stage to retry.

    Routing by artifacts present: transcript -> re-score (TRANSCRIBED); audio only
    -> re-transcribe (DOWNLOADED); neither -> re-download (NEW). Calls at/over the
    retry cap are left FAILED (dead-letter).
    """
    stats = RetryStats()
    async with session_scope() as session:
        failed = (
            await session.scalars(
                select(Call)
                .where(Call.status == CallStatus.FAILED)
                .order_by(Call.updated_at.asc())
                .limit(limit),
            )
        ).all()
        if not failed:
            return stats

        ids = [c.id for c in failed]
        with_transcript = set(
            (
                await session.scalars(
                    select(Transcript.call_id).where(Transcript.call_id.in_(ids)),
                )
            ).all(),
        )

        for call in failed:
            if call.attempts >= settings.max_retries:
                stats.dead_lettered += 1
                continue
            if call.id in with_transcript:
                call.status = CallStatus.TRANSCRIBED
                stats.to_transcribed += 1
            elif call.audio_object_key:
                call.status = CallStatus.DOWNLOADED
                stats.to_downloaded += 1
            else:
                call.status = CallStatus.NEW
                stats.to_new += 1
            call.error = None
            stats.requeued += 1

    logger.info(
        "Requeue: {n} reset (NEW={a} DOWNLOADED={b} TRANSCRIBED={c}); dead-letter={d}",
        n=stats.requeued,
        a=stats.to_new,
        b=stats.to_downloaded,
        c=stats.to_transcribed,
        d=stats.dead_lettered,
    )
    return stats


async def dead_letter(*, limit: int = 100) -> list[dict[str, Any]]:
    """FAILED calls that exhausted retries — for manual review."""
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(Call)
                .where(
                    Call.status == CallStatus.FAILED,
                    Call.attempts >= settings.max_retries,
                )
                .order_by(Call.updated_at.desc())
                .limit(limit),
            )
        ).all()
    return [
        {
            "call_id": c.id,
            "bitrix_call_id": c.bitrix_call_id,
            "attempts": c.attempts,
            "error": c.error,
        }
        for c in rows
    ]
