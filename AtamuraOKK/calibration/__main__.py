"""Calibration gate CLI: AI meeting scores vs human OKK xlsx (ТЗ §8).

``python -m AtamuraOKK.calibration --xlsx "Чек лист встречи ОП - Январь.xlsx"``
loads the human-graded meetings, pulls the AI ``scores`` rows for the meeting
rubric from the DB, joins them by CRM deal id, and prints a PASS/REVISE/FAIL
agreement report. Exit code 0 only on PASS — usable as a deploy gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from AtamuraOKK.calibration.db_source import ai_scores_by_deal
from AtamuraOKK.calibration.harness import compare
from AtamuraOKK.calibration.xlsx_loader import load_human_calls
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.scoring.rubric import load_rubric
from AtamuraOKK.settings import settings


async def run(*, xlsx_path: str, rubric_version: str, pass_threshold: int) -> int:
    """Run the calibration gate; return process exit code (0 = PASS)."""
    rubric = load_rubric(rubric_version)
    human = load_human_calls(xlsx_path)
    async with session_scope() as session:
        ai = await ai_scores_by_deal(session, rubric_version=rubric_version)

    report = compare(
        human,
        ai,
        max_total=rubric.max_total_score,
        pass_threshold=pass_threshold,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))  # noqa: T201
    return 0 if report.verdict == "PASS" else 1


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(prog="python -m AtamuraOKK.calibration")
    parser.add_argument("--xlsx", required=True, help="human-graded OKK xlsx path")
    parser.add_argument(
        "--rubric",
        default=None,
        help="rubric version (default: meeting rubric from settings)",
    )
    parser.add_argument("--pass-threshold", type=int, default=None)
    args = parser.parse_args()

    rubric_version = args.rubric or settings.score_meeting_rubric_version
    threshold = (
        args.pass_threshold
        if args.pass_threshold is not None
        else settings.score_pass_threshold
    )
    raise SystemExit(
        asyncio.run(
            run(
                xlsx_path=args.xlsx,
                rubric_version=rubric_version,
                pass_threshold=threshold,
            ),
        ),
    )


if __name__ == "__main__":
    main()
