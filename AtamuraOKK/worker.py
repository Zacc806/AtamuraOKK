"""Unified always-on production worker: ``python -m AtamuraOKK.worker``.

Runs the whole pipeline on a schedule in a single long-lived process, so a
production deployment is one container/service rather than several cron jobs:

  * ingestion full pass  — every ``worker_ingest_interval_hours`` (ingest ->
    requalify -> download), then transcribe + score the freshly downloaded calls
  * auto-recovery        — requeue FAILED calls every ``worker_retry_interval_hours``
  * QA reports           — lunch (first half) and evening (second half), local tz
  * daily run-summary    — at end-of-day, pushed via the alerter

Every job is guarded with ``max_instances=1`` + ``coalesce=True`` so a long run
never overlaps itself, and each job body catches its own exceptions so one
failure can't take the scheduler down. Stop with Ctrl-C / SIGTERM.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

from AtamuraOKK.ops.alert import get_alerter
from AtamuraOKK.ops.retry import requeue_failed
from AtamuraOKK.ops.summary import build_summary, render_summary
from AtamuraOKK.reporting.worker import generate_report
from AtamuraOKK.settings import settings


async def _pipeline_pass() -> None:
    """Ingest new calls, then transcribe and score everything ready."""
    from AtamuraOKK.ingestion.download import download_pending  # noqa: PLC0415
    from AtamuraOKK.ingestion.service import (  # noqa: PLC0415
        refresh_qualification,
        run_ingestion,
    )
    from AtamuraOKK.scoring.worker import score_pending  # noqa: PLC0415
    from AtamuraOKK.transcription.worker import transcribe_pending  # noqa: PLC0415

    logger.info("Worker: pipeline pass (ingest -> download -> transcribe -> score)")
    await run_ingestion()
    await refresh_qualification()
    await download_pending()
    await transcribe_pending()
    # score_pending defaults to the auto window (today-only per settings).
    await score_pending()


async def _job_pipeline() -> None:
    try:
        await _pipeline_pass()
    except Exception:
        logger.exception("Worker: pipeline pass failed")


async def _job_retry() -> None:
    try:
        stats = await requeue_failed()
        if stats.dead_lettered >= settings.alert_failure_threshold:
            await get_alerter().send(
                f"⚠️ Atamura QA: {stats.dead_lettered} звонков в dead-letter "
                f"(исчерпаны попытки). Требуется разбор.",
            )
    except Exception:
        logger.exception("Worker: retry pass failed")


async def _job_report(half: str) -> None:
    try:
        # run_pipeline=True so the report reflects calls finished since last pass.
        await generate_report(half, run_pipeline=True)
    except Exception:
        logger.exception("Worker: {half} report failed", half=half)


async def _job_daily_summary() -> None:
    try:
        summary = await build_summary()
        text = render_summary(summary)
        logger.info("\n{text}", text=text)
        if settings.worker_send_daily_summary:
            await get_alerter().send(text)
    except Exception:
        logger.exception("Worker: daily summary failed")


def _build_scheduler() -> AsyncIOScheduler:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: PLC0415
    from apscheduler.triggers.cron import CronTrigger  # noqa: PLC0415
    from apscheduler.triggers.interval import IntervalTrigger  # noqa: PLC0415

    scheduler = AsyncIOScheduler(timezone=settings.report_timezone)
    job_defaults = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 3600}

    scheduler.add_job(
        _job_pipeline,
        IntervalTrigger(hours=settings.worker_ingest_interval_hours),
        id="pipeline",
        **job_defaults,
    )
    scheduler.add_job(
        _job_retry,
        IntervalTrigger(hours=settings.worker_retry_interval_hours),
        id="retry",
        **job_defaults,
    )
    scheduler.add_job(
        _job_report,
        CronTrigger(hour=settings.report_lunch_hour, minute=0),
        args=["morning"],
        id="report-morning",
        **job_defaults,
    )
    scheduler.add_job(
        _job_report,
        CronTrigger(hour=settings.report_evening_hour, minute=0),
        args=["afternoon"],
        id="report-afternoon",
        **job_defaults,
    )
    # Daily summary a few minutes after the evening report so its numbers are final.
    scheduler.add_job(
        _job_daily_summary,
        CronTrigger(hour=settings.report_day_end_hour, minute=30),
        id="daily-summary",
        **job_defaults,
    )
    return scheduler


async def _run() -> None:
    scheduler = _build_scheduler()

    if settings.worker_run_on_start:
        logger.info("Worker: initial pipeline pass on startup")
        await _job_retry()
        await _job_pipeline()

    scheduler.start()
    logger.info(
        "Worker started ({tz}): pipeline every {ih}h, retry every {rh}h, "
        "reports {lunch}:00/{evening}:00, summary {end}:30. Ctrl-C to stop.",
        tz=settings.report_timezone,
        ih=settings.worker_ingest_interval_hours,
        rh=settings.worker_retry_interval_hours,
        lunch=settings.report_lunch_hour,
        evening=settings.report_evening_hour,
        end=settings.report_day_end_hour,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # e.g. Windows
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    logger.info("Worker: shutdown signal received; stopping scheduler.")
    scheduler.shutdown(wait=True)


def main() -> None:
    """Entry point for ``python -m AtamuraOKK.worker``."""
    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Worker stopped.")


if __name__ == "__main__":
    main()
