"""One-off backfill: download + transcribe ALL May 2026 calls (>=90s, RU + KZ).

Analyzable May calls flow through the normal dispatcher pipeline. This script
drives the NON-analyzable rest (SKIPPED/NEW with analyzable=false) through
download_one/transcribe_one directly, never setting analyzable=true, so the
score stage never claims them — they end at TRANSCRIBED without LLM scoring.

Billing-aware: probes Yandex with a single call first and sleeps while the
cloud is unbilled (PERMISSION_DENIED); billing failures reset attempts so they
never dead-letter. On billing recovery it restarts the docker transcribe
worker and requeues PERMISSION_DENIED dead-letters pipeline-wide.

Run from the repo root:  uv run python scripts/backfill_may.py
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import func, select, update

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.ingestion.download import download_one
from AtamuraOKK.settings import settings
from AtamuraOKK.storage import get_storage
from AtamuraOKK.transcription.worker import _load_transcriber, transcribe_one

# May 2026 in Asia/Qyzylorda (+05)
MAY_START = datetime(2026, 4, 30, 19, 0, tzinfo=UTC)
MAY_END = datetime(2026, 5, 31, 19, 0, tzinfo=UTC)

BILLING_SIGNATURE = "PERMISSION_DENIED"
BILLING_POLL_SECONDS = 600


def _target() -> tuple:
    """Predicate for rows this script owns: May, non-analyzable."""
    return (
        Call.started_at >= MAY_START,
        Call.started_at < MAY_END,
        Call.analyzable.is_(False),
    )


async def _claim(src: list[CallStatus], dst: CallStatus, limit: int, **extra) -> list[int]:
    """Atomically flip up to `limit` target rows src -> dst; return their ids."""
    async with session_scope() as session:
        stmt = (
            select(Call.id)
            .where(Call.status.in_(src), *_target())
            .order_by(Call.started_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        ids = list((await session.execute(stmt)).scalars().all())
        if ids:
            await session.execute(
                update(Call)
                .where(Call.id.in_(ids))
                .values(status=dst, claimed_at=func.now(), **extra)
            )
    return ids


async def _count(*where) -> int:
    async with session_scope() as session:
        return (
            await session.scalar(select(func.count()).select_from(Call).where(*where))
        ) or 0


async def run_downloads(concurrency: int) -> None:
    """SKIPPED/NEW (no audio yet) -> DOWNLOADED, via download_one."""
    sem = asyncio.Semaphore(concurrency)

    async def one(call_id: int) -> str:
        async with sem:
            return await download_one(call_id)

    done = failed = 0
    while True:
        async with session_scope() as session:
            stmt = (
                select(Call.id)
                .where(
                    Call.status.in_([CallStatus.SKIPPED, CallStatus.NEW]),
                    Call.audio_object_key.is_(None),
                    *_target(),
                )
                .order_by(Call.started_at)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
            ids = list((await session.execute(stmt)).scalars().all())
            if ids:
                await session.execute(
                    update(Call)
                    .where(Call.id.in_(ids))
                    .values(status=CallStatus.DOWNLOADING, claimed_at=func.now())
                )
        if not ids:
            break
        results = await asyncio.gather(*(one(i) for i in ids))
        done += sum(r == CallStatus.DOWNLOADED.value for r in results)
        failed += sum(r == CallStatus.FAILED.value for r in results)
        logger.info("download progress: +{n} ok={d} failed={f}", n=len(ids), d=done, f=failed)

    # one retry pass for transient download failures (under the cap)
    retry_ids = await _claim(
        [CallStatus.FAILED], CallStatus.DOWNLOADING, 10_000
    ) if failed else []
    # only retry rows that never got audio (download failures, not transcription)
    if retry_ids:
        async with session_scope() as session:
            keep = list(
                (
                    await session.execute(
                        select(Call.id).where(
                            Call.id.in_(retry_ids),
                            Call.audio_object_key.is_(None),
                            Call.attempts < settings.max_retries,
                        )
                    )
                ).scalars()
            )
            drop = set(retry_ids) - set(keep)
            if drop:  # not ours to retry — put them back to FAILED
                await session.execute(
                    update(Call).where(Call.id.in_(drop)).values(status=CallStatus.FAILED)
                )
        for cid in keep:
            await one(cid)
    logger.info("downloads finished: ok={d} failed={f}", d=done, f=failed)


async def _probe_billing(transcriber, storage) -> bool:
    """Transcribe ONE target call; True if billing works. Billing failures
    are reset (status back to DOWNLOADED, attempt refunded)."""
    ids = await _claim([CallStatus.DOWNLOADED], CallStatus.TRANSCRIBING, 1)
    if not ids:
        return True  # nothing left to probe with; let the main loop decide
    status = await transcribe_one(ids[0], transcriber=transcriber, storage=storage)
    if status != CallStatus.FAILED.value:
        return True
    async with session_scope() as session:
        call = await session.get(Call, ids[0])
        if call and BILLING_SIGNATURE in (call.error or ""):
            call.status = CallStatus.DOWNLOADED
            call.attempts = max(0, call.attempts - 1)
            call.claimed_at = None
            return False
    return True  # non-billing failure: billing itself is fine


async def _requeue_billing_failures() -> int:
    """Reset attempts on ALL PERMISSION_DENIED failures (incl. June dead-letters)
    so the pipeline retries them now that billing is back."""
    async with session_scope() as session:
        result = await session.execute(
            update(Call)
            .where(
                Call.status == CallStatus.FAILED,
                Call.error.like(f"%{BILLING_SIGNATURE}%"),
                Call.audio_object_key.is_not(None),
            )
            .values(status=CallStatus.DOWNLOADED, attempts=0, error=None, claimed_at=None)
        )
        return result.rowcount or 0


def _restart_transcribe_worker() -> None:
    try:
        subprocess.run(
            ["docker", "compose", "start", "transcribe"],
            cwd="/home/zakir/AtamuraOKK", check=True, capture_output=True, timeout=120,
        )
        logger.info("docker transcribe worker restarted")
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not restart transcribe worker: {e}", e=exc)


async def run_transcriptions(concurrency: int) -> None:
    transcriber = await asyncio.to_thread(_load_transcriber)
    storage = get_storage()
    sem = asyncio.Semaphore(concurrency)

    async def one(call_id: int) -> str:
        async with sem:
            return await transcribe_one(call_id, transcriber=transcriber, storage=storage)

    # wait out the billing outage before doing anything in bulk
    billing_restored_handled = False
    while not await _probe_billing(transcriber, storage):
        logger.warning(
            "Yandex billing still dry (PERMISSION_DENIED) — sleeping {s}s",
            s=BILLING_POLL_SECONDS,
        )
        await asyncio.sleep(BILLING_POLL_SECONDS)

    done = failed = 0
    while True:
        # small batches: claimed rows must finish inside claim_stale_seconds_transcribe
        ids = await _claim([CallStatus.DOWNLOADED], CallStatus.TRANSCRIBING, concurrency * 4)
        if not ids:
            # billing died mid-run? refund + go back to probing
            n = await _count(
                Call.status == CallStatus.FAILED,
                Call.error.like(f"%{BILLING_SIGNATURE}%"),
                *_target(),
            )
            if n:
                async with session_scope() as session:
                    await session.execute(
                        update(Call)
                        .where(
                            Call.status == CallStatus.FAILED,
                            Call.error.like(f"%{BILLING_SIGNATURE}%"),
                            *_target(),
                        )
                        .values(status=CallStatus.DOWNLOADED, attempts=0, claimed_at=None)
                    )
                logger.warning("billing died mid-run; {n} refunded, re-probing", n=n)
                while not await _probe_billing(transcriber, storage):
                    await asyncio.sleep(BILLING_POLL_SECONDS)
                continue
            # transient (non-billing) failures under the retry cap: requeue once more
            async with session_scope() as session:
                result = await session.execute(
                    update(Call)
                    .where(
                        Call.status == CallStatus.FAILED,
                        Call.attempts < settings.max_retries,
                        Call.audio_object_key.is_not(None),
                        *_target(),
                    )
                    .values(status=CallStatus.DOWNLOADED, claimed_at=None)
                )
                if result.rowcount:
                    logger.info("requeued {n} transient failures", n=result.rowcount)
                    continue
            break
        if not billing_restored_handled:
            # first successful bulk round: bring the rest of the pipeline back
            billing_restored_handled = True
            _restart_transcribe_worker()
            requeued = await _requeue_billing_failures()
            logger.info("billing OK — requeued {n} PERMISSION_DENIED dead-letters", n=requeued)
        results = await asyncio.gather(*(one(i) for i in ids))
        done += sum(r == CallStatus.TRANSCRIBED.value for r in results)
        failed += sum(r == CallStatus.FAILED.value for r in results)
        remaining = await _count(Call.status == CallStatus.DOWNLOADED, *_target())
        logger.info(
            "transcribe progress: +{n} ok={d} failed={f} remaining~{r}",
            n=len(ids), d=done, f=failed, r=remaining,
        )

    if billing_restored_handled is False:
        # we never transcribed in bulk (nothing to do) — still restore the pipeline
        _restart_transcribe_worker()
        await _requeue_billing_failures()
    logger.info("transcriptions finished: ok={d} failed={f}", d=done, f=failed)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-concurrency", type=int, default=5)
    parser.add_argument("--transcribe-concurrency", type=int, default=3)
    parser.add_argument("--skip-downloads", action="store_true")
    args = parser.parse_args()

    total = await _count(*_target())
    logger.info("backfill target: {n} non-analyzable May calls", n=total)

    if not args.skip_downloads:
        await run_downloads(args.download_concurrency)
    await run_transcriptions(args.transcribe_concurrency)

    transcribed = await _count(Call.status == CallStatus.TRANSCRIBED, *_target())
    failed = await _count(Call.status == CallStatus.FAILED, *_target())
    logger.info(
        "backfill done: transcribed={t} failed={f} of {n}",
        t=transcribed, f=failed, n=total,
    )


if __name__ == "__main__":
    asyncio.run(main())
