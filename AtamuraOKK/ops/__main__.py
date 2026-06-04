"""Ops CLI: ``python -m AtamuraOKK.ops <command>``.

summary      print the daily run-summary (optionally send via Telegram)
retry        requeue FAILED calls (under the retry cap) for another attempt
dead-letter  list FAILED calls that exhausted retries
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date

from loguru import logger

from AtamuraOKK.ops.alert import get_alerter
from AtamuraOKK.ops.retry import dead_letter, requeue_failed
from AtamuraOKK.ops.summary import build_summary, render_summary
from AtamuraOKK.settings import settings


def _cmd_summary(args: argparse.Namespace) -> None:
    async def run() -> None:
        day = date.fromisoformat(args.date) if args.date else None
        summary = await build_summary(day)
        text = render_summary(summary)
        logger.info("\n{text}", text=text)
        if args.send:
            await get_alerter().send(text)

    asyncio.run(run())


def _cmd_retry(args: argparse.Namespace) -> None:
    async def run() -> None:
        stats = await requeue_failed(limit=args.limit)
        if stats.dead_lettered >= settings.alert_failure_threshold:
            await get_alerter().send(
                f"⚠️ Atamura QA: {stats.dead_lettered} звонков в dead-letter "
                f"(исчерпаны попытки). Требуется разбор.",
            )

    asyncio.run(run())


def _cmd_dead_letter(args: argparse.Namespace) -> None:
    async def run() -> None:
        rows = await dead_letter(limit=args.limit)
        logger.info("Dead-letter: {n} calls", n=len(rows))
        for r in rows:
            logger.info(
                "  call {id} ({bx}) attempts={a}: {err}",
                id=r["call_id"],
                bx=r["bitrix_call_id"],
                a=r["attempts"],
                err=(r["error"] or "")[:120],
            )

    asyncio.run(run())


def main() -> None:
    """Parse args and dispatch."""
    parser = argparse.ArgumentParser(prog="python -m AtamuraOKK.ops")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sum = sub.add_parser("summary", help="daily run-summary")
    p_sum.add_argument("--date", help="YYYY-MM-DD (default: today)")
    p_sum.add_argument("--send", action="store_true", help="also send via Telegram")
    p_sum.set_defaults(func=_cmd_summary)

    p_retry = sub.add_parser("retry", help="requeue FAILED calls")
    p_retry.add_argument("--limit", type=int, default=500)
    p_retry.set_defaults(func=_cmd_retry)

    p_dl = sub.add_parser("dead-letter", help="list exhausted FAILED calls")
    p_dl.add_argument("--limit", type=int, default=100)
    p_dl.set_defaults(func=_cmd_dead_letter)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
