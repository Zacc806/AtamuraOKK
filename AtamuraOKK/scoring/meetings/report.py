"""Export scored ОП meetings to a CSV the QA team can open in Excel.

Reads the SCORED rows from the self-contained SQLite and flattens each stored
``ScoreResult`` (score, pass/fail, tone, red flags, summary) into one CSV row.
No Postgres, no network — just turns the pipeline's state into a readable report.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from loguru import logger

from AtamuraOKK.scoring.meetings.config import config
from AtamuraOKK.scoring.meetings.store import MeetingStore

_COLUMNS = [
    "file_id",
    "name",
    "folder_path",
    "meeting_at",
    "duration_sec",
    "score_pct",
    "passed",
    "call_type",
    "manager_tone",
    "needs_human_review",
    "red_flags",
    "summary",
]


def _record(row: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(row["score_json"]) if row["score_json"] else {}
    return {
        "file_id": row["file_id"],
        "name": row["name"],
        "folder_path": row["folder_path"],
        "meeting_at": row["meeting_at"],
        "duration_sec": row["duration_sec"],
        "score_pct": row["score_pct"],
        "passed": bool(row["passed"]),
        "call_type": data.get("call_type", ""),
        "manager_tone": data.get("manager_tone", ""),
        "needs_human_review": bool(data.get("needs_human_review", False)),
        "red_flags": "; ".join(data.get("red_flags", []) or []),
        "summary": data.get("summary", ""),
    }


def _report_path(out_path: Path | None) -> Path:
    if out_path is not None:
        return out_path
    raw = Path(config.meetings_report_path)
    return raw if raw.is_absolute() else config.meetings_work_dir / raw


def export_scored(
    out_path: Path | None = None,
    *,
    store: MeetingStore | None = None,
) -> dict[str, Any]:
    """Write every SCORED meeting to CSV; return a small summary dict."""
    own_store = store is None
    store = store or MeetingStore()
    try:
        records = [_record(r) for r in store.scored()]
    finally:
        if own_store:
            store.close()

    out = _report_path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel opens the Cyrillic columns without mojibake.
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_COLUMNS)
        writer.writeheader()
        writer.writerows(records)

    n = len(records)
    avg = round(sum(r["score_pct"] or 0 for r in records) / n, 1) if n else 0.0
    passed = sum(1 for r in records if r["passed"])
    summary = {
        "scored": n,
        "avg_score_pct": avg,
        "passed": passed,
        "pass_rate_pct": round(passed / n * 100, 1) if n else 0.0,
        "csv": str(out),
    }
    logger.info("Meeting report: {s}", s=summary)
    return summary
