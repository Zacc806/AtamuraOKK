"""Scoring worker CLI: TRANSCRIBED -> SCORED.

``python -m AtamuraOKK.scoring`` — score each transcribed call against the
configured rubric (Anthropic Claude by default) and persist the score.
"""

from __future__ import annotations

import argparse
import asyncio

from loguru import logger
from sqlalchemy import select

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.scoring.router import build_scorer
from AtamuraOKK.scoring.service import Outcome, ScoringService
from AtamuraOKK.settings import settings


async def score_pending(*, limit: int = 50) -> int:
    """Score a batch of TRANSCRIBED calls. Returns count scored."""
    service = ScoringService(
        scorer=build_scorer(),
        rubric_version=settings.score_rubric_version,
        min_duration_sec=settings.score_min_duration_sec,
        short_contact_min_sec=settings.short_contact_min_sec,
    )
    scored = 0
    async with session_scope() as session:
        calls = (
            await session.scalars(
                select(Call)
                .where(Call.status == CallStatus.TRANSCRIBED)
                .order_by(Call.started_at.asc())
                .limit(limit),
            )
        ).all()
        for call in calls:
            outcome = await service.score_call(session, call)
            if outcome.outcome is Outcome.SCORED:
                scored += 1
    logger.info("score: scored {n} calls", n=scored)
    return scored


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(prog="python -m AtamuraOKK.scoring")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    asyncio.run(score_pending(limit=args.limit))


if __name__ == "__main__":
    main()
