"""CLI entrypoint for the Phase 0 spike: ``python -m AtamuraOKK.spike <stage>``."""

from __future__ import annotations

import argparse
import asyncio

from AtamuraOKK.settings import settings


def _cmd_fetch(args: argparse.Namespace) -> None:
    from AtamuraOKK.spike.fetch import fetch_sample, save  # noqa: PLC0415

    calls = asyncio.run(
        fetch_sample(sample_size=args.size, days_back=args.days),
    )
    save(calls)


def _cmd_download(_: argparse.Namespace) -> None:
    from AtamuraOKK.spike.download import download_all  # noqa: PLC0415

    asyncio.run(download_all())


def _cmd_transcribe(_: argparse.Namespace) -> None:
    from AtamuraOKK.spike.transcribe import transcribe_all  # noqa: PLC0415

    transcribe_all()


def _cmd_wer(_: argparse.Namespace) -> None:
    from AtamuraOKK.spike.wer import compute, report  # noqa: PLC0415

    report(compute())


def _cmd_wazzup_probe(_: argparse.Namespace) -> None:
    from AtamuraOKK.spike.wazzup_probe import run_probe  # noqa: PLC0415

    asyncio.run(run_probe())


def _cmd_glossary_sample(args: argparse.Namespace) -> None:
    from AtamuraOKK.spike.glossary_sample import run_sample  # noqa: PLC0415

    run_sample(limit=args.limit)


def main() -> None:
    """Parse args and dispatch to the selected spike stage."""
    parser = argparse.ArgumentParser(
        prog="python -m AtamuraOKK.spike",
        description=f"Phase 0 transcription spike (output dir: {settings.spike_dir})",
    )
    sub = parser.add_subparsers(dest="stage", required=True)

    p_fetch = sub.add_parser("fetch", help="pull recent answered+recorded calls")
    p_fetch.add_argument("--size", type=int, default=60, help="sample size")
    p_fetch.add_argument("--days", type=int, default=7, help="look-back window")
    p_fetch.set_defaults(func=_cmd_fetch)

    sub.add_parser(
        "download",
        help="download recordings from CALL_RECORD_URL (disk scope only as fallback)",
    ).set_defaults(func=_cmd_download)

    sub.add_parser(
        "transcribe",
        help="run faster-whisper (needs spike group + ffmpeg)",
    ).set_defaults(func=_cmd_transcribe)

    sub.add_parser(
        "wer",
        help="compute WER vs hand-corrected references",
    ).set_defaults(func=_cmd_wer)

    sub.add_parser(
        "wazzup-probe",
        help="probe the Wazzup API to discover the calls/recordings surface",
    ).set_defaults(func=_cmd_wazzup_probe)

    p_gloss = sub.add_parser(
        "glossary-sample",
        help="LLM-correct existing meeting transcripts; dump before→after diffs",
    )
    p_gloss.add_argument("--limit", type=int, default=20, help="transcripts to sample")
    p_gloss.set_defaults(func=_cmd_glossary_sample)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
