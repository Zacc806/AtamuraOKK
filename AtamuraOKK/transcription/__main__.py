"""Transcription worker CLI: ``python -m AtamuraOKK.transcription <command>``."""

from __future__ import annotations

import argparse
import asyncio


def _cmd_run(args: argparse.Namespace) -> None:
    from AtamuraOKK.transcription.worker import transcribe_pending  # noqa: PLC0415

    asyncio.run(transcribe_pending(limit=args.limit))


def main() -> None:
    """Parse args and dispatch."""
    parser = argparse.ArgumentParser(prog="python -m AtamuraOKK.transcription")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="transcribe analyzable DOWNLOADED calls")
    p_run.add_argument("--limit", type=int, default=50)
    p_run.set_defaults(func=_cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
