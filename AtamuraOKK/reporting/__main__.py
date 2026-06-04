"""Reporting CLI: ``python -m AtamuraOKK.reporting <command>``.

generate   one half-day report (morning|afternoon)
schedule   run twice daily: lunch -> first half, evening -> second half
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date

from loguru import logger

from AtamuraOKK.reporting.worker import generate_report
from AtamuraOKK.settings import settings


def _cmd_generate(args: argparse.Namespace) -> None:
    day = date.fromisoformat(args.date) if args.date else None
    asyncio.run(
        generate_report(args.half, day=day, run_pipeline=args.run_pipeline),
    )


def _cmd_schedule(args: argparse.Namespace) -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: PLC0415
    from apscheduler.triggers.cron import CronTrigger  # noqa: PLC0415

    async def main() -> None:
        scheduler = AsyncIOScheduler(timezone=settings.report_timezone)
        # Lunch: report the first half of the day.
        scheduler.add_job(
            generate_report,
            CronTrigger(hour=settings.report_lunch_hour, minute=0),
            kwargs={"half": "morning", "run_pipeline": True},
            id="report-morning",
            max_instances=1,
            coalesce=True,
        )
        # Evening: report the second half of the day.
        scheduler.add_job(
            generate_report,
            CronTrigger(hour=settings.report_evening_hour, minute=0),
            kwargs={"half": "afternoon", "run_pipeline": True},
            id="report-afternoon",
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        logger.info(
            "Reports scheduled ({tz}): {lunch}:00 first half, "
            "{evening}:00 second half. Ctrl-C to stop.",
            tz=settings.report_timezone,
            lunch=settings.report_lunch_hour,
            evening=settings.report_evening_hour,
        )
        await asyncio.Event().wait()

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Report scheduler stopped.")


def main() -> None:
    """Parse args and dispatch."""
    parser = argparse.ArgumentParser(prog="python -m AtamuraOKK.reporting")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="generate one half-day report")
    p_gen.add_argument("--half", choices=["morning", "afternoon"], required=True)
    p_gen.add_argument("--date", help="YYYY-MM-DD (default: today in report tz)")
    p_gen.add_argument(
        "--run-pipeline",
        action="store_true",
        help="ingest/transcribe/score new calls before reporting",
    )
    p_gen.set_defaults(func=_cmd_generate)

    sub.add_parser(
        "schedule",
        help="run twice daily (lunch=first half, evening=second half)",
    ).set_defaults(func=_cmd_schedule)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
