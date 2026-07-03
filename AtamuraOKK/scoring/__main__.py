"""Scoring worker CLI: ``python -m AtamuraOKK.scoring <command>``."""

from __future__ import annotations

import argparse
import asyncio


def _cmd_run(args: argparse.Namespace) -> None:
    from AtamuraOKK.scoring.worker import score_pending  # noqa: PLC0415

    if args.all:
        # score the whole backlog, including older calls
        asyncio.run(score_pending(limit=args.limit, since=None))
    else:
        # default: the auto window (today-only when score_auto_today_only is set)
        asyncio.run(score_pending(limit=args.limit))


def _cmd_seed(_: argparse.Namespace) -> None:
    from AtamuraOKK.scoring.seed import seed_active_rubrics  # noqa: PLC0415

    asyncio.run(seed_active_rubrics())


def _cmd_requeue_relabel(args: argparse.Namespace) -> None:
    from AtamuraOKK.scoring.worker import requeue_scored_for_relabel  # noqa: PLC0415

    asyncio.run(
        requeue_scored_for_relabel(limit=args.limit, stereo_only=not args.include_mono),
    )


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

    p_relabel = sub.add_parser(
        "requeue-relabel",
        help="revert SCORED calls to TRANSCRIBED so a re-score reconciles inverted "
        "client/manager transcript labels (then run 'score --all')",
    )
    p_relabel.add_argument("--limit", type=int, default=None)
    p_relabel.add_argument(
        "--include-mono",
        action="store_true",
        help="also requeue mono calls (default: stereo only — the only calls whose "
        "channel->role mapping can invert)",
    )
    p_relabel.set_defaults(func=_cmd_requeue_relabel)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
