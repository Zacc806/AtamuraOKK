"""Scoring worker CLI: TRANSCRIBED -> SCORED.

``python -m AtamuraOKK.scoring`` scores transcribed telephony calls against the
call rubric (tm_call_v3). ``python -m AtamuraOKK.scoring --kind meeting`` scores
ОП face-to-face meetings (``calls.source == 'op_meeting'``) against the meeting
rubric (okk_meeting_v1) via the chunking map-reduce scorer. Both use Anthropic
Claude Sonnet and persist a ``scores`` row distinguished by ``rubric_version``.
"""

from __future__ import annotations

import argparse
import asyncio

from loguru import logger
from sqlalchemy import select

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallSource, CallStatus
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.scoring.router import build_meeting_scorer, build_scorer
from AtamuraOKK.scoring.service import Outcome, ScoringService
from AtamuraOKK.settings import settings


def _build_service(*, kind: str) -> tuple[ScoringService, CallSource]:
    """Build the scoring service + the call source it consumes for ``kind``."""
    if kind == "meeting":
        # Meetings have no telephony duration; gate only on empty transcript.
        service = ScoringService(
            scorer=build_meeting_scorer(),
            rubric_version=settings.score_meeting_rubric_version,
            min_duration_sec=0,
            short_contact_min_sec=0,
        )
        return service, CallSource.OP_MEETING
    service = ScoringService(
        scorer=build_scorer(),
        rubric_version=settings.score_rubric_version,
        min_duration_sec=settings.score_min_duration_sec,
        short_contact_min_sec=settings.short_contact_min_sec,
    )
    return service, CallSource.BITRIX_CALL


async def score_pending(*, kind: str = "call", limit: int = 50) -> int:
    """Score a batch of TRANSCRIBED rows of the given kind. Returns count scored."""
    service, source = _build_service(kind=kind)
    scored = 0
    async with session_scope() as session:
        calls = (
            await session.scalars(
                select(Call)
                .where(
                    Call.status == CallStatus.TRANSCRIBED,
                    Call.source == source,
                )
                .order_by(Call.started_at.asc())
                .limit(limit),
            )
        ).all()
        for call in calls:
            outcome = await service.score_call(session, call)
            if outcome.outcome is Outcome.SCORED:
                scored += 1
    logger.info("score[{kind}]: scored {n} rows", kind=kind, n=scored)
    return scored


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(prog="python -m AtamuraOKK.scoring")
    parser.add_argument("--kind", choices=("call", "meeting"), default="call")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    asyncio.run(score_pending(kind=args.kind, limit=args.limit))


if __name__ == "__main__":
    main()
