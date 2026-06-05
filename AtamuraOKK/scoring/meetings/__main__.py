"""Standalone ОП-meeting scoring CLI: transcript in -> okk_meeting_v1 score JSON.

``python -m AtamuraOKK.scoring.meetings --file meeting.txt`` (or pipe via stdin)
scores a speaker-tagged meeting transcript with Anthropic Claude and prints the
result as JSON. Parallel automation — touches no DB and none of the call-scoring
code. Needs ATAMURAOKK_ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from AtamuraOKK.scoring.meetings.base import CallForScoring
from AtamuraOKK.scoring.meetings.router import build_meeting_scorer


async def _score(text: str, duration_sec: int) -> str:
    scorer = build_meeting_scorer()
    result = await scorer.score(CallForScoring(text=text, duration_sec=duration_sec))
    import json  # noqa: PLC0415

    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(prog="python -m AtamuraOKK.scoring.meetings")
    parser.add_argument(
        "--file",
        help="speaker-tagged transcript file ('-' or omit reads stdin)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=0,
        help="meeting duration in seconds (optional context)",
    )
    args = parser.parse_args()
    text = (
        Path(args.file).read_text(encoding="utf-8")
        if args.file and args.file != "-"
        else sys.stdin.read()
    )
    print(asyncio.run(_score(text, args.duration)))  # noqa: T201


if __name__ == "__main__":
    main()
