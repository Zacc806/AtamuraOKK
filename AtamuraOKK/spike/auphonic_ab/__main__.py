"""CLI: ``python -m AtamuraOKK.spike.auphonic_ab <select|run|export>``.

Typical flow:

    python -m AtamuraOKK.spike.auphonic_ab select          # build manifest
    python -m AtamuraOKK.spike.auphonic_ab run --limit 1   # smoke-test one call
    python -m AtamuraOKK.spike.auphonic_ab run             # full 50
    python -m AtamuraOKK.spike.auphonic_ab export          # write A/B report
"""

from __future__ import annotations

import argparse
import asyncio


def _cmd_select(_: argparse.Namespace) -> None:
    from AtamuraOKK.spike.auphonic_ab.select import select_calls  # noqa: PLC0415

    asyncio.run(select_calls())


def _cmd_run(args: argparse.Namespace) -> None:
    from AtamuraOKK.spike.auphonic_ab.runner import run  # noqa: PLC0415

    asyncio.run(run(limit=args.limit, concurrency=args.concurrency, resume=args.resume))


def _cmd_export(args: argparse.Namespace) -> None:
    from AtamuraOKK.spike.auphonic_ab.export import export  # noqa: PLC0415

    export(ready_only=args.ready)


def _cmd_label_init(args: argparse.Namespace) -> None:
    from AtamuraOKK.spike.auphonic_ab.wer import label_init  # noqa: PLC0415

    label_init(count=args.count, prefill=args.prefill)


def _cmd_wer(_: argparse.Namespace) -> None:
    from AtamuraOKK.spike.auphonic_ab.wer import compute  # noqa: PLC0415

    compute()


def main() -> None:
    """Parse args and dispatch."""
    parser = argparse.ArgumentParser(prog="python -m AtamuraOKK.spike.auphonic_ab")
    sub = parser.add_subparsers(dest="command", required=True)

    p_select = sub.add_parser("select", help="build the 50-call manifest (read-only)")
    p_select.set_defaults(func=_cmd_select)

    p_run = sub.add_parser("run", help="fetch + transcribe before/after Auphonic")
    p_run.add_argument(
        "--limit", type=int, default=None, help="process only the first N calls"
    )
    p_run.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="calls to process in parallel (default: 4)",
    )
    p_run.add_argument(
        "--resume",
        action="store_true",
        help="skip calls already completed in results.json (reprocess only failures)",
    )
    p_run.set_defaults(func=_cmd_run)

    p_export = sub.add_parser("export", help="write the A/B report (csv + markdown)")
    p_export.add_argument(
        "--ready",
        action="store_true",
        help="export only fully-completed calls into out/ready/",
    )
    p_export.set_defaults(func=_cmd_export)

    p_label = sub.add_parser(
        "label-init", help="write blank reference templates for WER labeling"
    )
    p_label.add_argument(
        "--count", type=int, default=12, help="how many calls to label (default: 12)"
    )
    p_label.add_argument(
        "--prefill",
        action="store_true",
        help="seed refs from the 'before' transcript (faster but biases WER)",
    )
    p_label.set_defaults(func=_cmd_label_init)

    p_wer = sub.add_parser("wer", help="score before/after WER+CER vs filled refs")
    p_wer.set_defaults(func=_cmd_wer)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
