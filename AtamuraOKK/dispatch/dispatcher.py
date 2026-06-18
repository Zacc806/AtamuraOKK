"""Dispatcher: the singleton beat that feeds the per-stage worker queues.

On each tick it (1) reverts crashed workers' stale claims, (2) runs the
*singleton* ingestion + requalification pass (never fanned out — they share one
cursor), then (3) claims ready rows per stage and enqueues one task per call onto
that stage's queue. Postgres stays the source of truth: the claim is what
prevents double-processing, so a wiped Redis just means the next tick re-enqueues
everything still in a ready status.

Reports, retry/auto-recovery and the daily summary stay singleton cron jobs here
(they are aggregate operations, not per-call work).
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from AtamuraOKK.dispatch.claim import (
    STAGES,
    auto_since,
    claim_ready,
    reclaim_all_stale,
)
from AtamuraOKK.dispatch.tasks import STAGE_TASKS, queue_for
from AtamuraOKK.ingestion.service import refresh_qualification, run_ingestion
from AtamuraOKK.ops.alert import get_alerter
from AtamuraOKK.ops.retry import requeue_failed
from AtamuraOKK.ops.summary import build_summary, render_summary
from AtamuraOKK.reporting.worker import generate_report
from AtamuraOKK.settings import settings


async def dispatch_tick(ctx: dict[str, Any]) -> int:
    """One dispatch pass; returns the number of tasks enqueued."""
    redis = ctx["redis"]
    await reclaim_all_stale()

    try:
        await run_ingestion()
    except Exception:
        logger.exception("dispatch: ingestion pass failed")

    enqueued = 0
    for stage in STAGES:
        since = auto_since() if stage.today_only else None
        try:
            call_ids = await claim_ready(
                stage.ready,
                stage.in_flight,
                settings.claim_batch_size,
                since=since,
            )
        except Exception:
            logger.exception("dispatch: claim failed for {s}", s=stage.name)
            continue
        task_name = STAGE_TASKS[stage.name].__name__
        for call_id in call_ids:
            await redis.enqueue_job(
                task_name,
                call_id,
                _queue_name=queue_for(stage.name),
            )
        if call_ids:
            logger.info(
                "dispatch: enqueued {n} {s} task(s)", n=len(call_ids), s=stage.name
            )
        enqueued += len(call_ids)
    return enqueued


async def requalify_job(ctx: dict[str, Any]) -> None:
    """Promote SKIPPED first-calls whose client has since qualified.

    Its own cron, not part of the tick: one Bitrix round-trip per skipped client
    makes this pass minutes-long, and inside the tick it starves claim/enqueue
    past the arq job timeout.
    """
    try:
        await refresh_qualification()
    except Exception:
        logger.exception("dispatch: requalification pass failed")


async def retry_job(ctx: dict[str, Any]) -> None:
    """Auto-recovery: requeue FAILED calls, alert on dead-letters."""
    try:
        stats = await requeue_failed()
        if stats.dead_lettered >= settings.alert_failure_threshold:
            await get_alerter().send(
                f"⚠️ Atamura QA: {stats.dead_lettered} звонков в dead-letter "
                f"(исчерпаны попытки). Требуется разбор.",
            )
    except Exception:
        logger.exception("dispatch: retry pass failed")


async def _report(half: str) -> None:
    try:
        # The dispatcher already runs the pipeline continuously, so no pre-pass.
        await generate_report(half)
    except Exception:
        logger.exception("dispatch: {half} report failed", half=half)


async def report_morning(ctx: dict[str, Any]) -> None:
    """Generate the first-half (morning) QA report."""
    await _report("morning")


async def report_afternoon(ctx: dict[str, Any]) -> None:
    """Generate the second-half (afternoon) QA report."""
    await _report("afternoon")


async def daily_summary(ctx: dict[str, Any]) -> None:
    """Build and (optionally) send the end-of-day run summary."""
    try:
        summary = await build_summary()
        text = render_summary(summary)
        logger.info("\n{text}", text=text)
        if settings.worker_send_daily_summary:
            await get_alerter().send(text)
    except Exception:
        logger.exception("dispatch: daily summary failed")
