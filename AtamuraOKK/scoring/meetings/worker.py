"""Always-on scheduler for the ОП meeting-recording pipeline.

``python -m AtamuraOKK.scoring.meetings.worker`` runs the whole meeting pipeline
on a schedule in one long-lived process — the parallel counterpart of the call
pipeline's ``AtamuraOKK.worker``, but fully self-contained (own config, own SQLite
state, no Postgres, no imports of the call pipeline). Two jobs:

  * pipeline pass — ingest → download → transcribe → score every
    ``meetings_worker_interval_hours``
  * retry pass   — re-queue FAILED recordings every
    ``meetings_worker_retry_interval_hours``

Each job is ``max_instances=1`` + ``coalesce=True`` and guards its own exceptions,
so one slow/failed run never overlaps itself or takes the scheduler down. Stop
with Ctrl-C / SIGTERM.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import TYPE_CHECKING

from loguru import logger

from AtamuraOKK.scoring.meetings.config import config
from AtamuraOKK.scoring.meetings.recordings import requeue_failed, run_pipeline

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler


async def _job_pipeline() -> None:
    try:
        await run_pipeline()
    except Exception:
        logger.exception("Meeting worker: pipeline pass failed")


async def _job_retry() -> None:
    try:
        await requeue_failed()
    except Exception:
        logger.exception("Meeting worker: retry pass failed")


def _build_scheduler() -> AsyncIOScheduler:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: PLC0415
    from apscheduler.triggers.interval import IntervalTrigger  # noqa: PLC0415

    scheduler = AsyncIOScheduler(timezone=config.meetings_worker_timezone)
    job_defaults = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 3600}
    scheduler.add_job(
        _job_pipeline,
        IntervalTrigger(hours=config.meetings_worker_interval_hours),
        id="meetings-pipeline",
        **job_defaults,
    )
    scheduler.add_job(
        _job_retry,
        IntervalTrigger(hours=config.meetings_worker_retry_interval_hours),
        id="meetings-retry",
        **job_defaults,
    )
    return scheduler


async def _run() -> None:
    scheduler = _build_scheduler()

    if config.meetings_worker_run_on_start:
        logger.info("Meeting worker: initial pipeline pass on startup")
        # Retry first so a backlog of FAILED rows left by a previous run (e.g. an
        # Anthropic-credit outage) is re-queued *before* this startup pass, which
        # then reprocesses them. On a fresh DB the retry is simply a no-op.
        await _job_retry()
        await _job_pipeline()

    scheduler.start()
    logger.info(
        "Meeting worker started ({tz}): pipeline every {ih}h, retry every {rh}h. "
        "Ctrl-C to stop.",
        tz=config.meetings_worker_timezone,
        ih=config.meetings_worker_interval_hours,
        rh=config.meetings_worker_retry_interval_hours,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # e.g. Windows
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    logger.info("Meeting worker: shutdown signal received; stopping scheduler.")
    scheduler.shutdown(wait=True)


def main() -> None:
    """Entry point for ``python -m AtamuraOKK.scoring.meetings.worker``."""
    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Meeting worker stopped.")


if __name__ == "__main__":
    main()
