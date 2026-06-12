"""One-off retroactive rescope to the until-qualified rule (May 1 -> now).

Operator decision 2026-06-12: the until-qualified scope rule (see
ingestion/service.py::_apply_scope) ships forward-only — this script is the
explicit one-time override that re-opens the frozen legacy verdicts
(not_first_call / not_qualified / qualification_unknown) for the window and
re-applies the new rule:

  in scope  = started_at <= client_qualified_at (or client never/unknown qualified)
  skipped   = after_qualification

Effects per row (window only):
  - every call gets client_qualified / client_qualified_at stamped
  - NEW/SKIPPED:   in scope -> NEW + analyzable=true (pipeline scores them)
                   out      -> SKIPPED + after_qualification
  - rows advanced by the May backfill with analyzable=false
    (DOWNLOADING/DOWNLOADED/TRANSCRIBING/TRANSCRIBED):
                   in scope -> analyzable=true (dispatcher picks them up where
                               they stand, incl. scoring of TRANSCRIBED)
                   out      -> skip_reason=after_qualification, stays unscored
  - SCORED / FAILED / analyzable=true in-flight rows: stamp only, never demoted

MUST run only after the until-qualified migration is applied AND the dispatcher
runs the new-rule code (an old-rule dispatcher would demote promotions back).

Dry-run by default:  PYTHONPATH=. uv run python scripts/rescope_until_qualified.py
Apply:               ... --execute
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select

from AtamuraOKK.bitrix import BitrixClient
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.ingestion.qualification import (
    UNKNOWN_QUALIFICATION,
    Qualification,
    default_checker,
)
from AtamuraOKK.settings import settings

WINDOW_START = datetime(2026, 4, 30, 19, 0, tzinfo=UTC)  # 2026-05-01 00:00 +05

_RESCOPABLE = (CallStatus.NEW, CallStatus.SKIPPED)
_BACKFILL_ADVANCED = (
    CallStatus.DOWNLOADING,
    CallStatus.DOWNLOADED,
    CallStatus.TRANSCRIBING,
    CallStatus.TRANSCRIBED,
)
_CHUNK = 200


def _in_scope(call: Call, qual: Qualification) -> bool:
    if call.duration_sec < settings.ingest_min_duration_sec:
        return False
    return not (
        qual.at is not None
        and call.started_at is not None
        and call.started_at > qual.at
    )


def _rescope_call(call: Call, qual: Qualification, stats: Counter) -> None:
    call.client_qualified = qual.qualified
    call.client_qualified_at = qual.at
    in_scope = _in_scope(call, qual)

    if call.status in _RESCOPABLE:
        if in_scope:
            if not (call.status is CallStatus.NEW and call.analyzable):
                stats["promoted_to_new"] += 1
            call.analyzable = True
            call.status = CallStatus.NEW
            call.skip_reason = None
        else:
            if call.analyzable or call.skip_reason != "after_qualification":
                stats["skipped_after_qualification"] += 1
            call.analyzable = False
            call.status = CallStatus.SKIPPED
            call.skip_reason = "after_qualification"
    elif not call.analyzable and call.status in _BACKFILL_ADVANCED:
        if in_scope:
            call.analyzable = True
            call.skip_reason = None
            stats["promoted_in_flight"] += 1
            if call.status is CallStatus.TRANSCRIBED:
                stats["promoted_transcribed_to_score"] += 1
        else:
            call.skip_reason = "after_qualification"
            stats["flagged_after_qualification_done"] += 1
    else:
        stats["stamped_only"] += 1


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="apply (default: dry-run)")
    args = parser.parse_args()

    async with session_scope() as session:
        keys = list(
            (
                await session.execute(
                    select(Call.client_key)
                    .where(Call.started_at >= WINDOW_START, Call.client_key.is_not(None))
                    .distinct(),
                )
            ).scalars()
        )
    logger.info("rescope window has {n} distinct clients", n=len(keys))

    checker = default_checker()
    stats: Counter = Counter()
    async with BitrixClient() as bx:
        for i in range(0, len(keys), _CHUNK):
            chunk = set(keys[i : i + _CHUNK])
            quals = await checker.qualified(chunk, bx)
            async with session_scope() as session:
                calls = (
                    await session.scalars(
                        select(Call)
                        .where(
                            Call.client_key.in_(chunk),
                            Call.started_at >= WINDOW_START,
                        )
                        .with_for_update(),
                    )
                ).all()
                for call in calls:
                    _rescope_call(call, quals.get(call.client_key) or UNKNOWN_QUALIFICATION, stats)
                if not args.execute:
                    await session.rollback()
            logger.info(
                "progress {done}/{n} clients: {s}",
                done=min(i + _CHUNK, len(keys)), n=len(keys), s=dict(stats),
            )

    mode = "APPLIED" if args.execute else "DRY-RUN (nothing written)"
    logger.info("rescope {mode}: {s}", mode=mode, s=dict(stats))


if __name__ == "__main__":
    asyncio.run(main())
