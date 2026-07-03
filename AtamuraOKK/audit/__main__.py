"""CLI for the close-reason audit pass.

    python -m AtamuraOKK.audit run [--limit N]

Runs one incremental pass (or a bounded slice with ``--limit``). Backfill the whole
history by running ``run`` with no limit repeatedly until ``scanned=0``. The pass is
idempotent, so re-running is safe.
"""

from __future__ import annotations

import argparse
import asyncio

from loguru import logger

from AtamuraOKK.audit.service import run_audit
from AtamuraOKK.bitrix import BitrixClient
from AtamuraOKK.db.session import session_scope


async def _run(limit: int | None) -> None:
    async with session_scope() as session, BitrixClient() as bx:
        stats = await run_audit(session, bx, limit=limit)
    logger.info("audit done: {s}", s=stats)


def main() -> None:
    """Parse args and run the requested audit subcommand."""
    parser = argparse.ArgumentParser(prog="AtamuraOKK.audit")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one incremental audit pass")
    run.add_argument("--limit", type=int, default=None, help="max deals to scan")
    args = parser.parse_args()
    if args.command == "run":
        asyncio.run(_run(args.limit))


if __name__ == "__main__":
    main()
