#!/usr/bin/env python3
"""Export transcribed ОП meeting recordings for one month to JSON.

Reads the meeting pipeline's self-contained SQLite state and dumps every
recording that has a transcript (status TRANSCRIBED or later) whose meeting
falls in the given month — including the full transcript, which the regular
``report`` CSV and the Postgres ``meetings`` table both omit.

Stdlib only, so it runs on the deploy host without the project env.

    python scripts/export_transcribed_meetings.py                 # May 2026
    python scripts/export_transcribed_meetings.py --month 2025-05
    python scripts/export_transcribed_meetings.py --db /path/to/meetings.db -o out.json

The month filter uses the real meeting time (``meeting_at``, parsed from the
filename), falling back to the Disk upload time (``created_at``) when the
filename carried no date.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Default DB: <repo>/.meetings/meetings.db (this file lives in <repo>/scripts).
DEFAULT_DB = Path(__file__).resolve().parents[1] / ".meetings" / "meetings.db"

_FIELDS = (
    "file_id",
    "name",
    "folder_path",
    "status",
    "created_by",
    "meeting_at",
    "created_at",
    "duration_sec",
    "language",
    "score_pct",
    "passed",
    "transcript",
)


def export(
    db_path: Path,
    out_path: Path,
    *,
    month: str | None = None,
    exclude_whatsapp: bool = False,
) -> int:
    """Write transcribed recordings (optionally scoped) to ``out_path``."""
    clauses = ["transcript IS NOT NULL", "transcript <> ''"]
    params: list[object] = []
    if month:
        clauses.append("substr(COALESCE(meeting_at, created_at), 1, 7) = ?")
        params.append(month)
    if exclude_whatsapp:
        clauses.append("LOWER(name) NOT LIKE '%whatsapp%'")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT * FROM recordings
        WHERE {" AND ".join(clauses)}
        ORDER BY COALESCE(meeting_at, created_at) ASC, file_id ASC
        """,  # noqa: S608 (clauses are static fragments; params bound)
        params,
    ).fetchall()
    conn.close()

    records = []
    for r in rows:
        rec = {k: r[k] for k in _FIELDS}
        rec["passed"] = None if r["passed"] is None else bool(r["passed"])
        records.append(rec)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return len(records)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--month", default=None, help="YYYY-MM filter (default: all)")
    p.add_argument(
        "--exclude-whatsapp",
        action="store_true",
        help="skip WhatsApp voice notes (keep only real meeting recordings)",
    )
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="meetings.db path")
    p.add_argument("-o", "--out", type=Path, help="output JSON path")
    args = p.parse_args()

    if not args.db.exists():
        sys.exit(f"meetings DB not found: {args.db}")

    scope = args.month or ("meetings" if args.exclude_whatsapp else "all")
    out = args.out or (args.db.parent / f"transcribed_meetings_{scope}.json")
    n = export(
        args.db,
        out,
        month=args.month,
        exclude_whatsapp=args.exclude_whatsapp,
    )
    print(f"exported {n} transcribed recordings ({scope}) -> {out}")  # noqa: T201


if __name__ == "__main__":
    main()
