"""ОП-meeting scoring CLI.

Two modes:

* **Pipeline** — pull ОП meeting recordings from the "Встречи ОП" Bitrix Disk
  folder and score them end-to-end::

      python -m AtamuraOKK.scoring.meetings run         # full pass (all stages)
      python -m AtamuraOKK.scoring.meetings ingest      # Disk scan → register NEW
      python -m AtamuraOKK.scoring.meetings download    # NEW → DOWNLOADED
      python -m AtamuraOKK.scoring.meetings transcribe  # DOWNLOADED → TRANSCRIBED
      python -m AtamuraOKK.scoring.meetings score       # TRANSCRIBED → SCORED
      python -m AtamuraOKK.scoring.meetings status      # print state counts

* **One transcript** (legacy) — score a single speaker-tagged transcript::

      python -m AtamuraOKK.scoring.meetings --file meeting.txt

Touches no Postgres and none of the call-scoring code. Needs the Anthropic key
(scoring) and the Bitrix webhook (Disk ingestion) in ``.env``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from AtamuraOKK.scoring.meetings.base import CallForScoring
from AtamuraOKK.scoring.meetings.router import build_meeting_scorer

_PIPELINE_CMDS = frozenset(
    {"ingest", "download", "transcribe", "score", "run", "status"},
)


async def _score_transcript(text: str, duration_sec: int) -> str:
    scorer = build_meeting_scorer()
    result = await scorer.score(CallForScoring(text=text, duration_sec=duration_sec))
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)


async def _run_pipeline_cmd(cmd: str, limit: int | None) -> str:
    # Imported lazily so the legacy --file path needs no Disk/httpx deps.
    from AtamuraOKK.scoring.meetings import recordings  # noqa: PLC0415
    from AtamuraOKK.scoring.meetings.download import download_pending  # noqa: PLC0415
    from AtamuraOKK.scoring.meetings.store import open_store  # noqa: PLC0415
    from AtamuraOKK.scoring.meetings.transcribe import (  # noqa: PLC0415
        transcribe_pending,
    )

    if cmd == "status":
        with open_store() as store:
            return json.dumps(store.counts(), ensure_ascii=False, indent=2)
    if cmd == "ingest":
        return _fmt(await recordings.ingest_recordings(limit=limit))
    if cmd == "download":
        return _fmt(await download_pending(limit=limit))
    if cmd == "transcribe":
        return _fmt(await transcribe_pending(limit=limit))
    if cmd == "score":
        return _fmt(await recordings.score_pending(limit=limit))
    result = await recordings.run_pipeline(limit=limit)
    return _fmt(result)


def _fmt(obj: object) -> str:
    """Render a stats dataclass / dict as readable JSON."""
    if hasattr(obj, "__dict__"):
        return json.dumps(vars(obj), ensure_ascii=False, indent=2, default=vars)
    return json.dumps(obj, ensure_ascii=False, indent=2, default=vars)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m AtamuraOKK.scoring.meetings")
    parser.add_argument(
        "command",
        nargs="?",
        choices=sorted(_PIPELINE_CMDS),
        help="pipeline stage to run; omit to score a single --file transcript",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="max recordings to process this pass"
    )
    parser.add_argument(
        "--file", help="speaker-tagged transcript ('-' or omit reads stdin)"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=0,
        help="meeting duration in seconds (legacy --file mode)",
    )
    return parser


def main() -> None:
    """CLI entrypoint."""
    args = _build_parser().parse_args()

    if args.command in _PIPELINE_CMDS:
        print(asyncio.run(_run_pipeline_cmd(args.command, args.limit)))  # noqa: T201
        return

    text = (
        Path(args.file).read_text(encoding="utf-8")
        if args.file and args.file != "-"
        else sys.stdin.read()
    )
    if not text.strip():
        _build_parser().error("empty transcript (pass --file or pipe text via stdin)")
    print(asyncio.run(_score_transcript(text, args.duration)))  # noqa: T201


if __name__ == "__main__":
    main()
