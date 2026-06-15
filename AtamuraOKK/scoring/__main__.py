"""Scoring worker CLI: ``python -m AtamuraOKK.scoring <command>``."""

from __future__ import annotations

import argparse
import asyncio


def _cmd_run(args: argparse.Namespace) -> None:
    from AtamuraOKK.dispatch.claim import report_today_start  # noqa: PLC0415
    from AtamuraOKK.scoring.worker import score_pending  # noqa: PLC0415
    from AtamuraOKK.settings import settings  # noqa: PLC0415

    if args.all:
        since = None  # score the whole backlog, including older calls
    elif settings.score_auto_today_only:
        since = report_today_start()
    else:
        since = None
    asyncio.run(score_pending(limit=args.limit, since=since))


def _cmd_seed(_: argparse.Namespace) -> None:
    from AtamuraOKK.scoring.seed import seed_active_rubrics  # noqa: PLC0415

    asyncio.run(seed_active_rubrics())


def main() -> None:
    """Parse args and dispatch."""
    parser = argparse.ArgumentParser(prog="python -m AtamuraOKK.scoring")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="score analyzable TRANSCRIBED calls")
    p_run.add_argument("--limit", type=int, default=50)
    p_run.add_argument(
        "--all",
        action="store_true",
        help="score the full backlog, including calls from earlier days "
        "(default: only today's calls when score_auto_today_only is set)",
    )
    p_run.set_defaults(func=_cmd_run)

    sub.add_parser("seed", help="seed the active rubrics (tm + op)").set_defaults(
        func=_cmd_seed,
    )

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
