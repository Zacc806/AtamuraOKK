"""Ingestion worker CLI: ``python -m AtamuraOKK.ingestion <command>``.

Commands:
  ingest    one incremental pull (Bitrix -> Postgres)
  download  fetch analyzable calls' recordings -> object storage
  run       ingest then download (one full pass)
  schedule  run the full pass now, then every N hours (APScheduler)
"""

from __future__ import annotations

import argparse
import asyncio

from loguru import logger

from AtamuraOKK.ingestion.download import download_pending
from AtamuraOKK.ingestion.service import refresh_qualification, run_ingestion


async def _full_pass(ingest_limit: int | None, download_limit: int) -> None:
    await run_ingestion(limit=ingest_limit)
    await refresh_qualification()  # late-qual sync: skip post-qual calls early
    await download_pending(limit=download_limit)


def _cmd_ingest(args: argparse.Namespace) -> None:
    asyncio.run(run_ingestion(limit=args.limit))


def _cmd_download(args: argparse.Namespace) -> None:
    asyncio.run(download_pending(limit=args.limit))


def _cmd_requalify(args: argparse.Namespace) -> None:
    asyncio.run(refresh_qualification(limit=args.limit))


def _cmd_run(args: argparse.Namespace) -> None:
    asyncio.run(_full_pass(args.limit, args.download_limit))


def _cmd_schedule(args: argparse.Namespace) -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: PLC0415
    from apscheduler.triggers.interval import IntervalTrigger  # noqa: PLC0415

    async def main() -> None:
        await _full_pass(args.limit, args.download_limit)  # run once immediately
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            _full_pass,
            IntervalTrigger(hours=args.hours),
            args=[args.limit, args.download_limit],
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        logger.info("Scheduled ingestion every {h}h; Ctrl-C to stop.", h=args.hours)
        await asyncio.Event().wait()  # run forever

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Ingestion scheduler stopped.")


def main() -> None:
    """Parse args and dispatch."""
    parser = argparse.ArgumentParser(prog="python -m AtamuraOKK.ingestion")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="one incremental pull")
    p_ingest.add_argument("--limit", type=int, default=None)
    p_ingest.set_defaults(func=_cmd_ingest)

    p_download = sub.add_parser("download", help="download analyzable recordings")
    p_download.add_argument("--limit", type=int, default=200)
    p_download.set_defaults(func=_cmd_download)

    p_requal = sub.add_parser(
        "requalify",
        help="re-check pending first-calls; promote newly-qualified ones",
    )
    p_requal.add_argument("--limit", type=int, default=1000)
    p_requal.set_defaults(func=_cmd_requalify)

    p_run = sub.add_parser("run", help="ingest then download")
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--download-limit", type=int, default=200)
    p_run.set_defaults(func=_cmd_run)

    p_sched = sub.add_parser("schedule", help="run now, then every N hours")
    p_sched.add_argument("--hours", type=int, default=3)
    p_sched.add_argument("--limit", type=int, default=None)
    p_sched.add_argument("--download-limit", type=int, default=200)
    p_sched.set_defaults(func=_cmd_schedule)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
