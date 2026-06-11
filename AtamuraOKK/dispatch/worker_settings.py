"""arq ``WorkerSettings`` for the dispatcher and the per-stage worker pools.

Each role is a separate worker process bound to its own queue, so the CPU-bound
transcribe pool scales independently from the IO-bound download/score pools. Run
them via ``python -m AtamuraOKK.dispatch <role>`` (see ``__main__``).

Only this module (and ``__main__``) import arq, so the pipeline stages and the
legacy ``worker.py`` never require the optional ``broker`` dependency group.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings
from loguru import logger

from AtamuraOKK.dispatch.dispatcher import (
    daily_summary,
    dispatch_tick,
    report_afternoon,
    report_morning,
    requalify_job,
    retry_job,
)
from AtamuraOKK.dispatch.tasks import (
    download_task,
    queue_for,
    score_task,
    transcribe_task,
)
from AtamuraOKK.settings import settings
from AtamuraOKK.storage import get_storage


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


def _tick_seconds() -> set[int]:
    """Seconds-of-minute the dispatch tick fires on (see dispatch_interval_seconds)."""
    interval = settings.dispatch_interval_seconds
    if 0 < interval < 60 and 60 % interval == 0:
        return set(range(0, 60, interval))
    return {0}  # once per minute


def _job_timeout(stale_ttl: int) -> int:
    """Kill a job before its stale TTL so the reconciler never reverts a live job.

    If ``job_timeout >= stale_ttl`` a long-but-alive job is reclaimed and
    re-enqueued while still running, so it executes twice (duplicate paid work).
    """
    return max(60, stale_ttl - settings.claim_job_timeout_margin_seconds)


class DispatcherSettings:
    """Singleton beat: dispatch fan-out + retry/report/summary cron jobs."""

    redis_settings = _redis_settings()
    functions: ClassVar[list[Any]] = []
    cron_jobs: ClassVar[list[Any]] = [
        cron(dispatch_tick, second=_tick_seconds(), run_at_startup=True),
        # Requalification does one Bitrix round-trip per skipped client, so it
        # needs a timeout far above the 300s arq default.
        cron(requalify_job, minute={10, 40}, timeout=3600, run_at_startup=True),
        cron(retry_job, minute={0}),
        cron(report_morning, hour={settings.report_lunch_hour}, minute={0}),
        cron(report_afternoon, hour={settings.report_evening_hour}, minute={0}),
        cron(daily_summary, hour={settings.report_day_end_hour}, minute={30}),
    ]


class DownloadWorker:
    """IO-bound: fetch recordings to object storage."""

    redis_settings = _redis_settings()
    queue_name = queue_for("download")
    functions: ClassVar[list[Any]] = [download_task]
    max_jobs = settings.download_concurrency
    job_timeout = _job_timeout(settings.claim_stale_seconds_download)


async def _transcribe_startup(ctx: dict[str, Any]) -> None:
    from AtamuraOKK.transcription.worker import _load_transcriber  # noqa: PLC0415

    logger.info("transcribe worker: loading model")
    ctx["transcriber"] = await asyncio.to_thread(_load_transcriber)
    ctx["storage"] = get_storage()


class TranscribeWorker:
    """CPU-bound: split channels + whisper. Model is loaded once at startup."""

    redis_settings = _redis_settings()
    queue_name = queue_for("transcribe")
    functions: ClassVar[list[Any]] = [transcribe_task]
    max_jobs = settings.transcribe_concurrency
    job_timeout = _job_timeout(settings.claim_stale_seconds_transcribe)
    on_startup = _transcribe_startup


async def _score_startup(ctx: dict[str, Any]) -> None:
    from AtamuraOKK.scoring.factory import get_scorer  # noqa: PLC0415
    from AtamuraOKK.scoring.rubric import load_rubric  # noqa: PLC0415

    ctx["scorer"] = get_scorer()
    ctx["rubric"] = load_rubric()


class ScoreWorker:
    """IO-bound: LLM scoring. Scorer + rubric are loaded once at startup."""

    redis_settings = _redis_settings()
    queue_name = queue_for("score")
    functions: ClassVar[list[Any]] = [score_task]
    max_jobs = settings.score_concurrency
    job_timeout = _job_timeout(settings.claim_stale_seconds_score)
    on_startup = _score_startup


ROLES = {
    "dispatcher": DispatcherSettings,
    "download": DownloadWorker,
    "transcribe": TranscribeWorker,
    "score": ScoreWorker,
}
