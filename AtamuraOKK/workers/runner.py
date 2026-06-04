"""APScheduler runner wiring all worker jobs into one long-running process."""

from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from AtamuraOKK.bitrix import BitrixClient
from AtamuraOKK.bitrix.users import sync_users
from AtamuraOKK.db.dao.manager_dao import DepartmentDAO, ManagerDAO
from AtamuraOKK.scoring.router import build_scorer
from AtamuraOKK.settings import settings
from AtamuraOKK.transcription.router import build_transcriber
from AtamuraOKK.workers.context import build_engine, session_factory
from AtamuraOKK.workers.download import run_download
from AtamuraOKK.workers.ingest import run_ingest
from AtamuraOKK.workers.score import run_score
from AtamuraOKK.workers.transcribe import run_transcribe


async def _sync_users_job(factory: async_sessionmaker[AsyncSession]) -> None:
    async with BitrixClient() as bx, factory() as session:
        await sync_users(bx, ManagerDAO(session), DepartmentDAO(session))
        await session.commit()


def build_scheduler(factory: async_sessionmaker[AsyncSession]) -> AsyncIOScheduler:
    """Build the scheduler with all pipeline jobs registered."""
    transcriber = build_transcriber()
    scorer = build_scorer()
    scheduler = AsyncIOScheduler(timezone="UTC")
    common = {"max_instances": 1, "coalesce": True}
    pipeline_min = settings.pipeline_interval_min

    scheduler.add_job(
        run_ingest,
        "interval",
        minutes=settings.ingest_interval_min,
        args=[factory],
        id="ingest",
        **common,
    )
    scheduler.add_job(
        _sync_users_job,
        "interval",
        minutes=settings.user_sync_interval_min,
        args=[factory],
        id="sync_users",
        **common,
    )
    scheduler.add_job(
        run_download,
        "interval",
        minutes=pipeline_min,
        args=[factory],
        id="download",
        **common,
    )
    scheduler.add_job(
        run_transcribe,
        "interval",
        minutes=pipeline_min,
        args=[factory, transcriber],
        id="transcribe",
        **common,
    )
    scheduler.add_job(
        run_score,
        "interval",
        minutes=pipeline_min,
        args=[factory, scorer],
        id="score",
        **common,
    )
    return scheduler


async def run_forever() -> None:
    """Start the scheduler and run until cancelled."""
    engine = build_engine()
    factory = session_factory(engine)
    scheduler = build_scheduler(factory)
    scheduler.start()
    logger.info("AtamuraOKK workers started")
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)
        await engine.dispose()
