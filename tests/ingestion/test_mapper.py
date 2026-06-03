"""Tests for the Bitrix row -> Call mapper."""

from __future__ import annotations

from typing import Any

from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.ingestion.mapper import map_row

MIN = 15


def _row(**over: Any) -> dict[str, Any]:
    base = {
        "CALL_ID": "1408510",
        "PORTAL_USER_ID": "68534",
        "CALL_TYPE": "1",
        "CALL_DURATION": "120",
        "CALL_START_DATE": "2026-06-03T10:00:00+05:00",
        "CALL_FAILED_CODE": "200",
        "RECORD_FILE_ID": "9001",
        "CRM_ENTITY_TYPE": "DEAL",
        "CRM_ENTITY_ID": "436100",
    }
    base.update(over)
    return base


def test_answered_recorded_is_new() -> None:
    """An answered, long-enough, recorded call is NEW with mapped fields."""
    mapped = map_row(_row(), min_duration_sec=MIN)
    assert mapped.status == CallStatus.NEW
    assert mapped.bitrix_call_id == "1408510"
    assert mapped.bitrix_user_id == 68534
    assert mapped.direction == 1
    assert mapped.duration_sec == 120
    assert mapped.crm_entity_id == 436100
    assert mapped.started_at is not None
    assert mapped.started_at.year == 2026


def test_missed_call_is_skipped() -> None:
    """A missed call (failed code != 200) is SKIPPED."""
    mapped = map_row(_row(CALL_FAILED_CODE="304"), min_duration_sec=MIN)
    assert mapped.status == CallStatus.SKIPPED


def test_too_short_is_skipped() -> None:
    """A call shorter than the minimum is SKIPPED."""
    mapped = map_row(_row(CALL_DURATION="5"), min_duration_sec=MIN)
    assert mapped.status == CallStatus.SKIPPED


def test_no_recording_is_skipped() -> None:
    """A call with no recording url/file id is SKIPPED."""
    row = _row()
    del row["RECORD_FILE_ID"]
    mapped = map_row(row, min_duration_sec=MIN)
    assert mapped.status == CallStatus.SKIPPED


def test_direct_url_counts_as_recording() -> None:
    """A direct CALL_RECORD_URL (no file id) still counts as recorded."""
    row = _row()
    del row["RECORD_FILE_ID"]
    row["CALL_RECORD_URL"] = "https://storage/rec.mp3?token=x"
    mapped = map_row(row, min_duration_sec=MIN)
    assert mapped.status == CallStatus.NEW
    assert mapped.record_url is not None
