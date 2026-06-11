"""Scoring worker CLI: ``python -m AtamuraOKK.scoring <command>``."""

from __future__ import annotations

import argparse
import asyncio


def _cmd_run(args: argparse.Namespace) -> None:
    from AtamuraOKK.scoring.worker import score_pending  # noqa: PLC0415

    asyncio.run(score_pending(limit=args.limit))


def _cmd_seed(_: argparse.Namespace) -> None:
    from AtamuraOKK.scoring.seed import seed_active_rubrics  # noqa: PLC0415

    asyncio.run(seed_active_rubrics())


def main() -> None:
    """Parse args and dispatch."""
    parser = argparse.ArgumentParser(prog="python -m AtamuraOKK.scoring")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="score analyzable TRANSCRIBED calls")
    p_run.add_argument("--limit", type=int, default=50)
    p_run.set_defaults(func=_cmd_run)

    sub.add_parser("seed", help="seed the active rubrics (tm + op)").set_defaults(
        func=_cmd_seed,
    )

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
