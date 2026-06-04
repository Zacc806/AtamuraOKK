"""Sale-outcome backfill (ТЗ 3.4): tag a score with the deal's real result +30d.

``python -m AtamuraOKK.outcomes`` — for each scored contact whose CRM deal is at
least N days old, read the deal's stage from Bitrix and record won / lose /
pending in the score's ``meta``. This exposes high-scored-but-lost or
low-scored-but-won contacts and lets the rubric be recalibrated against real
sales. Needs the Bitrix webhook (scope ``crm``) + a populated DB to run.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import select

from AtamuraOKK.bitrix.client import BitrixClient
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.score import Score
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.settings import settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_WON_MARKERS = ("WON",)
_LOSE_MARKERS = ("LOSE", "APOLOGY")
_FINAL = frozenset({"won", "lose"})


def classify_stage(stage_id: str) -> str:
    """Map a Bitrix deal ``STAGE_ID`` to won | lose | pending.

    Bitrix success stages end in ``WON`` (e.g. ``C5:WON``) and failure stages in
    ``LOSE``/``APOLOGY``; anything else is still in progress.
    """
    stage = stage_id.upper()
    if any(m in stage for m in _WON_MARKERS):
        return "won"
    if any(m in stage for m in _LOSE_MARKERS):
        return "lose"
    return "pending"


async def _candidates(
    session: AsyncSession,
    cutoff: datetime,
    limit: int,
) -> list[tuple[Score, Call]]:
    rows = await session.execute(
        select(Score, Call)
        .join(Call, Score.call_id == Call.id)
        .where(
            Call.crm_entity_type == "DEAL",
            Call.crm_entity_id.is_not(None),
            Call.started_at <= cutoff,
        )
        .order_by(Call.started_at.asc())
        .limit(limit),
    )
    return [(score, call) for score, call in rows]


async def backfill_outcomes(
    *,
    now: datetime,
    older_than_days: int = 30,
    limit: int = 200,
    client: BitrixClient | None = None,
) -> int:
    """Tag eligible scores with their deal outcome. Returns the count updated."""
    cutoff = now - timedelta(days=older_than_days)
    bitrix = client or BitrixClient()
    updated = 0
    try:
        async with session_scope() as session:
            for score, call in await _candidates(session, cutoff, limit):
                meta = dict(score.meta or {})
                if meta.get("sale_outcome") in _FINAL:
                    continue  # already settled — don't re-query
                deal = await bitrix.call("crm.deal.get", {"id": call.crm_entity_id})
                stage = str((deal or {}).get("STAGE_ID", ""))
                meta["sale_outcome"] = classify_stage(stage)
                meta["deal_stage"] = stage
                meta["outcome_checked_at"] = now.isoformat()
                score.meta = meta
                updated += 1
    finally:
        if client is None:
            await bitrix.aclose()
    logger.info("outcomes: updated {n} scores", n=updated)
    return updated


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(prog="python -m AtamuraOKK.outcomes")
    parser.add_argument("--days", type=int, default=settings.outcome_check_days)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()
    asyncio.run(
        backfill_outcomes(
            now=datetime.now(UTC),
            older_than_days=args.days,
            limit=args.limit,
        ),
    )


if __name__ == "__main__":
    main()
