"""Meeting-recording pipeline: Disk → download → transcribe → score → SQLite.

Wires the self-contained stages together and feeds each transcript into the
existing :func:`build_meeting_scorer` (okk_meeting_v1). This is the production
counterpart of the ``--file`` CLI: instead of one pasted transcript, it pulls the
ОП meeting recordings straight from the "Встречи ОП" Disk folder and scores them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from loguru import logger

from AtamuraOKK.scoring.meetings.base import CallForScoring
from AtamuraOKK.scoring.meetings.config import config
from AtamuraOKK.scoring.meetings.disk import BitrixDisk, MeetingDiskSource
from AtamuraOKK.scoring.meetings.download import download_pending
from AtamuraOKK.scoring.meetings.router import build_meeting_scorer
from AtamuraOKK.scoring.meetings.store import MeetingStatus, MeetingStore
from AtamuraOKK.scoring.meetings.transcribe import transcribe_pending


@dataclass
class IngestStats:
    """Summary of one ingestion (Disk scan) pass."""

    scanned: int = 0
    new: int = 0


@dataclass
class ScoreStats:
    """Summary of one scoring pass."""

    attempted: int = 0
    scored: int = 0
    failed: int = 0


async def ingest_recordings(
    *,
    limit: int | None = None,
    store: MeetingStore | None = None,
    source: MeetingDiskSource | None = None,
) -> IngestStats:
    """Scan the Disk folder and register new audio/video recordings as NEW."""
    stats = IngestStats()
    own_store = store is None
    store = store or MeetingStore()
    disk: BitrixDisk | None = None
    if source is None:
        disk = BitrixDisk()
        source = MeetingDiskSource(disk)
    try:
        async for rec in source.iter_recordings(max_items=limit):
            stats.scanned += 1
            if store.upsert_new(rec):
                stats.new += 1
    finally:
        if disk is not None:
            await disk.aclose()
        if own_store:
            store.close()

    logger.info("Meeting ingest: scanned={s} new={n}", s=stats.scanned, n=stats.new)
    return stats


async def score_pending(
    *,
    limit: int | None = None,
    store: MeetingStore | None = None,
) -> ScoreStats:
    """Score TRANSCRIBED recordings against okk_meeting_v1 → SCORED."""
    stats = ScoreStats()
    limit = limit if limit is not None else config.meetings_batch_limit
    own_store = store is None
    store = store or MeetingStore()
    scorer = build_meeting_scorer()
    try:
        rows = store.claim(MeetingStatus.TRANSCRIBED, limit)
        logger.info("Scoring {n} meeting transcripts", n=len(rows))
        for row in rows:
            stats.attempted += 1
            file_id = int(row["file_id"])
            try:
                result = await scorer.score(
                    CallForScoring(
                        text=row["transcript"] or "",
                        duration_sec=int(row["duration_sec"] or 0),
                        language=row["language"] or "auto",
                        call_ref=f"{file_id}:{row['name']}",
                    ),
                )
                store.mark_scored(
                    file_id,
                    json.dumps(result.to_dict(), ensure_ascii=False),
                    result.score_pct,
                    passed=result.passed,
                )
                stats.scored += 1
            except Exception as exc:
                dead = store.bump_attempt(
                    file_id,
                    f"score: {exc}",
                    max_attempts=config.meetings_max_attempts,
                )
                stats.failed += int(dead)
                logger.warning(
                    "Meeting scoring failed for {id}: {e}", id=file_id, e=exc
                )
    finally:
        if own_store:
            store.close()

    logger.info(
        "Meeting scoring: attempted={a} scored={s} failed={f}",
        a=stats.attempted,
        s=stats.scored,
        f=stats.failed,
    )
    return stats


async def run_pipeline(*, limit: int | None = None) -> dict[str, object]:
    """One full pass: ingest → download → transcribe → score, sharing a store."""
    with MeetingStore() as store:
        ingest = await ingest_recordings(limit=limit, store=store)
        downloaded = await download_pending(limit=limit, store=store)
        transcribed = await transcribe_pending(limit=limit, store=store)
        scored = await score_pending(limit=limit, store=store)
        counts = store.counts()
    logger.info("Meeting pipeline done: {c}", c=counts)
    return {
        "ingest": ingest,
        "download": downloaded,
        "transcribe": transcribed,
        "score": scored,
        "counts": counts,
    }
