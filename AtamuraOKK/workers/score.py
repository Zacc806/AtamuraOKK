"""Scoring job: TRANSCRIBED -> SCORED via the language-routed scorer."""

from __future__ import annotations

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from AtamuraOKK.db.dao.call_dao import CallDAO
from AtamuraOKK.db.dao.score_dao import ScoreDAO
from AtamuraOKK.db.dao.transcript_dao import TranscriptDAO
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.scoring.base import Scorer
from AtamuraOKK.scoring.service import ScoringService
from AtamuraOKK.settings import settings


async def run_score(
    factory: async_sessionmaker[AsyncSession],
    scorer: Scorer,
) -> int:
    """Score a batch of TRANSCRIBED calls. Returns count scored."""
    scored = 0
    async with factory() as session:
        calls = CallDAO(session)
        service = ScoringService(
            scorer=scorer,
            calls=calls,
            transcripts=TranscriptDAO(session),
            scores=ScoreDAO(session),
            rubric_version=settings.score_rubric_version,
            min_duration_sec=settings.score_min_duration_sec,
            short_contact_min_sec=settings.short_contact_min_sec,
        )
        batch = await calls.claim_batch(
            CallStatus.TRANSCRIBED,
            settings.score_batch_size,
        )
        for call in batch:
            outcome = await service.score_call(call)
            if outcome.outcome.value == "SCORED":
                scored += 1
        await session.commit()
    logger.info("score: scored {n} calls", n=scored)
    return scored
