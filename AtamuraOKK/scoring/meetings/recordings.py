"""Meeting-recording pipeline: Disk → download → transcribe → score → Postgres.

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
from AtamuraOKK.scoring.meetings.push import push_pending
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
    """One full pass: ingest → download → transcribe → score → push to Postgres."""
    with MeetingStore() as store:
        ingest = await ingest_recordings(limit=limit, store=store)
        downloaded = await download_pending(limit=limit, store=store)
        transcribed = await transcribe_pending(limit=limit, store=store)
        scored = await score_pending(limit=limit, store=store)
        pushed = await push_pending(limit=limit, store=store)
        counts = store.counts()
    logger.info("Meeting pipeline done: {c}", c=counts)
    return {
        "ingest": ingest,
        "download": downloaded,
        "transcribe": transcribed,
        "score": scored,
        "push": pushed,
        "counts": counts,
    }


async def requeue_failed(*, store: MeetingStore | None = None) -> int:
    """Re-open FAILED recordings for another attempt; returns how many."""
    own_store = store is None
    store = store or MeetingStore()
    try:
        n = store.reset_failed()
    finally:
        if own_store:
            store.close()
    if n:
        logger.info("Meeting retry: re-queued {n} FAILED recordings", n=n)
    return n


async def rescore(
    *,
    all_scored: bool = False,
    store: MeetingStore | None = None,
) -> int:
    """Re-queue SCORED meetings for re-scoring (and re-pushing); returns count.

    By default only meetings whose transcript exceeds one chunk — the ones
    whose original score was computed from a truncated transcript before
    chunking worked. ``all_scored=True`` re-queues every scored meeting (e.g.
    after a rubric change).
    """
    threshold = None if all_scored else config.score_meeting_chunk_chars
    own_store = store is None
    store = store or MeetingStore()
    try:
        n = store.reset_for_rescore(min_transcript_chars=threshold)
    finally:
        if own_store:
            store.close()
    logger.info(
        "Meeting rescore: re-queued {n} SCORED recordings ({scope})",
        n=n,
        scope="all" if all_scored else f"transcript > {threshold} chars",
    )
    return n


async def drain_pipeline(
    *,
    limit: int | None = None,
    ingest: bool = True,
    max_passes: int = 1000,
) -> dict[str, object]:
    """Process the whole backlog: scan once, then loop the stages until drained.

    Stops when nothing is left in flight (NEW/DOWNLOADED/TRANSCRIBED all zero) or
    a pass makes no forward progress (e.g. every remaining row keeps failing on a
    transient outage), so it never spins forever.
    """
    with MeetingStore() as store:
        if ingest:
            await ingest_recordings(limit=None, store=store)
        passes = 0
        stalled = 0
        while passes < max_passes:
            passes += 1
            downloaded = await download_pending(limit=limit, store=store)
            transcribed = await transcribe_pending(limit=limit, store=store)
            scored = await score_pending(limit=limit, store=store)
            await push_pending(limit=limit, store=store)
            counts = store.counts()
            active = (
                counts.get(MeetingStatus.NEW.value, 0)
                + counts.get(MeetingStatus.DOWNLOADED.value, 0)
                + counts.get(MeetingStatus.TRANSCRIBED.value, 0)
            )
            progressed = downloaded.downloaded + transcribed.transcribed + scored.scored
            logger.info(
                "Drain pass {p}: active={a} progressed={g} {c}",
                p=passes,
                a=active,
                g=progressed,
                c=counts,
            )
            if active == 0:
                break
            # Tolerate one no-progress pass (a transient blip), but stop after two
            # in a row so a persistent outage (e.g. no Anthropic credit) can't spin.
            stalled = stalled + 1 if progressed == 0 else 0
            if stalled >= 2:
                break
        return {"passes": passes, "counts": store.counts()}
