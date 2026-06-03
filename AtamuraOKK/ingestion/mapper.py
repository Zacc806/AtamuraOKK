"""Map ``voximplant.statistic.get`` rows to Call fields + an ingest decision.

Pure (no DB/network), so the filtering rules are unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from AtamuraOKK.db.models.enums import CallStatus

_ANSWERED_CODE = "200"


@dataclass(slots=True)
class MappedCall:
    """Bitrix call row mapped to our column shape + ingest status."""

    bitrix_call_id: str
    bitrix_user_id: int | None
    direction: int | None
    started_at: datetime | None
    duration_sec: int
    failed_code: str | None
    record_file_id: str | None
    record_url: str | None
    crm_entity_type: str | None
    crm_entity_id: int | None
    crm_activity_id: int | None
    phone_number: str | None
    status: CallStatus


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def map_row(row: dict[str, Any], *, min_duration_sec: int) -> MappedCall:
    """Map one Bitrix row; mark NEW if scoreable, else SKIPPED.

    Scoreable = answered (CALL_FAILED_CODE 200) AND long enough AND has a
    recording (RECORD_FILE_ID or CALL_RECORD_URL).
    """
    duration = _to_int(row.get("CALL_DURATION")) or 0
    failed_code = _to_str(row.get("CALL_FAILED_CODE"))
    record_file_id = _to_str(row.get("RECORD_FILE_ID"))
    record_url = _to_str(row.get("CALL_RECORD_URL"))

    has_recording = bool(record_file_id or record_url)
    scoreable = (
        failed_code == _ANSWERED_CODE
        and duration >= min_duration_sec
        and has_recording
    )

    return MappedCall(
        bitrix_call_id=str(row.get("CALL_ID") or ""),
        bitrix_user_id=_to_int(row.get("PORTAL_USER_ID")),
        direction=_to_int(row.get("CALL_TYPE")),
        started_at=_parse_dt(row.get("CALL_START_DATE")),
        duration_sec=duration,
        failed_code=failed_code,
        record_file_id=record_file_id,
        record_url=record_url,
        crm_entity_type=_to_str(row.get("CRM_ENTITY_TYPE")),
        crm_entity_id=_to_int(row.get("CRM_ENTITY_ID")),
        crm_activity_id=_to_int(row.get("CRM_ACTIVITY_ID")),
        phone_number=_to_str(row.get("PHONE_NUMBER")),
        status=CallStatus.NEW if scoreable else CallStatus.SKIPPED,
    )
