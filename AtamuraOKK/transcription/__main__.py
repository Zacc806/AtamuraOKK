"""Transcription worker CLI: ``python -m AtamuraOKK.transcription <command>``."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path


def _cmd_run(args: argparse.Namespace) -> None:
    from AtamuraOKK.transcription.worker import transcribe_pending  # noqa: PLC0415

    asyncio.run(
        transcribe_pending(limit=args.limit, concurrency=args.concurrency),
    )


def _cmd_requeue_kk(args: argparse.Namespace) -> None:
    from AtamuraOKK.transcription.worker import requeue_pending_kk  # noqa: PLC0415

    asyncio.run(requeue_pending_kk(limit=args.limit))


def _cmd_progress(args: argparse.Namespace) -> None:
    from AtamuraOKK.transcription.progress import (  # noqa: PLC0415
        db_snapshot,
        parse_log,
        render,
    )

    async def snapshot_once() -> str:
        return render(parse_log(Path(args.log)), await db_snapshot())

    if not args.watch:
        sys.stdout.write(asyncio.run(snapshot_once()) + "\n")
        return
    try:
        while True:
            sys.stdout.write("\033[2J\033[H")  # clear screen
            sys.stdout.write(asyncio.run(snapshot_once()) + "\n")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


def main() -> None:
    """Parse args and dispatch."""
    parser = argparse.ArgumentParser(prog="python -m AtamuraOKK.transcription")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="transcribe analyzable DOWNLOADED calls")
    p_run.add_argument("--limit", type=int, default=50)
    p_run.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="calls in parallel (default: settings.transcribe_concurrency)",
    )
    p_run.set_defaults(func=_cmd_run)

    p_kk = sub.add_parser(
        "requeue-kk",
        help="revert parked PENDING_KK calls to DOWNLOADED (re-transcribe with kk)",
    )
    p_kk.add_argument("--limit", type=int, default=None)
    p_kk.set_defaults(func=_cmd_requeue_kk)

    p_prog = sub.add_parser("progress", help="show backlog transcription progress")
    p_prog.add_argument("--log", default=".transcribe_backlog.log")
    p_prog.add_argument("--watch", action="store_true", help="refresh continuously")
    p_prog.add_argument("--interval", type=float, default=5.0)
    p_prog.set_defaults(func=_cmd_progress)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
