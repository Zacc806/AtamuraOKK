"""Tests for the scored-meeting CSV export."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from AtamuraOKK.scoring.meetings.disk import MeetingFile
from AtamuraOKK.scoring.meetings.report import export_scored
from AtamuraOKK.scoring.meetings.store import MeetingStore


def _file(file_id: int, name: str) -> MeetingFile:
    """Build a MeetingFile fixture."""
    return MeetingFile(
        file_id=file_id,
        name=name,
        ext=".ogg",
        size=10,
        folder_path="Май",
        download_url="u",
        created_at="2026-06-03T10:00:00+03:00",
        meeting_at=datetime(2025, 5, 31, 15, 0, 0),
    )


def _score_json(score_pct: float, *, summary: str, flags: list[str]) -> str:
    return json.dumps(
        {
            "call_type": "первичный",
            "manager_tone": "вежливый",
            "needs_human_review": False,
            "red_flags": flags,
            "summary": summary,
            "score_pct": score_pct,
        },
        ensure_ascii=False,
    )


def _seed_scored(
    store: MeetingStore,
    file_id: int,
    name: str,
    pct: float,
    *,
    passed: bool,
    summary: str,
    flags: list[str],
) -> None:
    store.upsert_new(_file(file_id, name))
    store.mark_downloaded(file_id, "/a.ogg", 600)
    store.mark_transcribed(file_id, "[agent] привет", "ru")
    store.mark_scored(
        file_id,
        _score_json(pct, summary=summary, flags=flags),
        pct,
        passed=passed,
    )


def test_export_writes_csv_and_summary(tmp_path: Path) -> None:
    """export_scored writes one CSV row per SCORED meeting + a summary."""
    out = tmp_path / "report.csv"
    with MeetingStore(tmp_path / "m.db") as store:
        _seed_scored(
            store, 1, "a.ogg", 80.0, passed=True, summary="ок", flags=["обещал скидку"]
        )
        _seed_scored(store, 2, "b.ogg", 60.0, passed=False, summary="слабо", flags=[])
        # a non-scored row must not appear
        store.upsert_new(_file(3, "c.ogg"))

        summary = export_scored(out, store=store)

    assert summary["scored"] == 2
    assert summary["avg_score_pct"] == 70.0
    assert summary["passed"] == 1
    assert summary["pass_rate_pct"] == 50.0

    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert {r["file_id"] for r in rows} == {"1", "2"}
    row1 = next(r for r in rows if r["file_id"] == "1")
    assert row1["passed"] == "True"
    assert row1["red_flags"] == "обещал скидку"
    assert row1["summary"] == "ок"


def test_export_empty_store(tmp_path: Path) -> None:
    """With nothing scored, the CSV has only a header and a zeroed summary."""
    out = tmp_path / "report.csv"
    with MeetingStore(tmp_path / "m.db") as store:
        summary = export_scored(out, store=store)
    assert summary["scored"] == 0
    assert summary["avg_score_pct"] == 0.0
    assert out.exists()
