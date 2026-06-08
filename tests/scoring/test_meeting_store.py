"""Tests for the self-contained meeting-recording SQLite store."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from AtamuraOKK.scoring.meetings.disk import MeetingFile
from AtamuraOKK.scoring.meetings.store import MeetingStatus, MeetingStore


def _file(file_id: int = 1, *, name: str = "rec.ogg") -> MeetingFile:
    """Build a MeetingFile fixture."""
    return MeetingFile(
        file_id=file_id,
        name=name,
        ext=".ogg",
        size=1234,
        folder_path="Встречи ОП/Май",
        download_url="https://x/download",
        created_at="2026-06-03T10:00:00+03:00",
        meeting_at=datetime(2025, 5, 31, 15, 58, 2),
    )


def test_upsert_is_idempotent(tmp_path: Path) -> None:
    """Re-ingesting a known file id updates it, does not insert a new row."""
    with MeetingStore(tmp_path / "m.db") as store:
        assert store.upsert_new(_file()) is True
        assert store.upsert_new(_file(name="renamed.ogg")) is False
        rows = store.claim(MeetingStatus.NEW, 10)
        assert len(rows) == 1
        assert rows[0]["name"] == "renamed.ogg"  # ingestion-owned col refreshed


def test_status_transitions(tmp_path: Path) -> None:
    """A recording advances NEW → DOWNLOADED → TRANSCRIBED → SCORED."""
    with MeetingStore(tmp_path / "m.db") as store:
        store.upsert_new(_file())
        store.mark_downloaded(1, "/audio/1.ogg", 130)
        assert [r["file_id"] for r in store.claim(MeetingStatus.DOWNLOADED, 10)] == [1]

        store.mark_transcribed(1, "[agent] привет", "ru")
        assert store.get(1)["status"] == MeetingStatus.TRANSCRIBED.value

        store.mark_scored(1, '{"score_pct": 80.0}', 80.0, passed=True)
        row = store.get(1)
        assert row["status"] == MeetingStatus.SCORED.value
        assert row["score_pct"] == 80.0
        assert row["passed"] == 1


def test_bump_attempt_dead_letters_past_max(tmp_path: Path) -> None:
    """Attempts accumulate and flip the row to FAILED once max is reached."""
    with MeetingStore(tmp_path / "m.db") as store:
        store.upsert_new(_file())
        assert store.bump_attempt(1, "boom", max_attempts=3) is False
        assert store.bump_attempt(1, "boom", max_attempts=3) is False
        assert store.bump_attempt(1, "boom", max_attempts=3) is True
        row = store.get(1)
        assert row["status"] == MeetingStatus.FAILED.value
        assert row["attempts"] == 3
        assert "boom" in row["error"]


def test_claim_orders_by_meeting_time(tmp_path: Path) -> None:
    """claim() returns the earliest meeting first."""
    with MeetingStore(tmp_path / "m.db") as store:
        late = _file(2, name="b.ogg")
        late.meeting_at = datetime(2025, 6, 1, 9, 0, 0)
        store.upsert_new(late)
        store.upsert_new(_file(1, name="a.ogg"))  # earlier meeting_at
        order = [r["file_id"] for r in store.claim(MeetingStatus.NEW, 10)]
        assert order == [1, 2]


def test_counts_groups_by_status(tmp_path: Path) -> None:
    """counts() returns a per-status tally."""
    with MeetingStore(tmp_path / "m.db") as store:
        store.upsert_new(_file(1))
        store.upsert_new(_file(2, name="b.ogg"))
        store.mark_skipped(2, "too_short:5s")
        assert store.counts() == {"NEW": 1, "SKIPPED": 1}
