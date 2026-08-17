"""CLI for the close-reason audit pass.

    python -m AtamuraOKK.audit run [--limit N] [--live]
    python -m AtamuraOKK.audit poll [--wait]

``run`` does one incremental pass (or a bounded slice with ``--limit``). Backfill the
whole history by running it with no limit repeatedly until ``scanned=0``; the pass is
idempotent, so re-running is safe.

By default the judge route goes through the Batches API, so ``run`` *submits* and the
verdicts land on a later ``poll``. ``--live`` judges inline at full price instead, for
when you need the verdicts in front of you now. ``poll --wait`` blocks until every open
batch has been settled — the supervised submit-then-drain shape for a backfill.
"""

from __future__ import annotations

import argparse
import asyncio

from loguru import logger

from AtamuraOKK.audit.batch import poll_batches
from AtamuraOKK.audit.service import run_audit
from AtamuraOKK.bitrix import BitrixClient
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.settings import settings


async def _run(limit: int | None, *, live: bool) -> None:
    if live:
        settings.audit_judge_batch_enabled = False
    async with session_scope() as session, BitrixClient() as bx:
        stats = await run_audit(session, bx, limit=limit)
    logger.info("audit done: {s}", s=stats)


async def _poll(*, wait: bool, interval_seconds: int) -> None:
    while True:
        stats = await poll_batches()
        logger.info("audit poll: {s}", s=stats)
        if not wait or stats.open_batches - stats.ended <= 0:
            return
        await asyncio.sleep(interval_seconds)


def main() -> None:
    """Parse args and run the requested audit subcommand."""
    parser = argparse.ArgumentParser(prog="AtamuraOKK.audit")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one incremental audit pass")
    run.add_argument("--limit", type=int, default=None, help="max deals to scan")
    run.add_argument(
        "--live",
        action="store_true",
        help="judge inline at full price instead of submitting a batch",
    )
    poll = sub.add_parser("poll", help="settle finished judge batches")
    poll.add_argument(
        "--wait",
        action="store_true",
        help="keep polling until every open batch has been settled",
    )
    poll.add_argument(
        "--interval",
        type=int,
        default=60,
        help="seconds between polls when --wait is set",
    )
    args = parser.parse_args()
    if args.command == "run":
        asyncio.run(_run(args.limit, live=args.live))
    elif args.command == "poll":
        asyncio.run(_poll(wait=args.wait, interval_seconds=args.interval))


if __name__ == "__main__":
    main()
