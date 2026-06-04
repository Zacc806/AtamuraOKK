"""Translate a ``voximplant.statistic.get`` row into Call model fields."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from AtamuraOKK.db.models.enums import CallDirection

# Bitrix CALL_TYPE: 1 = outbound, 2 = inbound (verified against the live API).
_DIRECTION = {"1": CallDirection.OUTBOUND, "2": CallDirection.INBOUND}
_NON_DIGITS = re.compile(r"\D+")


def map_direction(call_type: Any) -> CallDirection:
    """Map Bitrix CALL_TYPE to our direction enum."""
    return _DIRECTION.get(str(call_type), CallDirection.UNKNOWN)


def _normalize_phone(phone: str) -> str:
    """Digits-only phone (KZ numbers vary between 8... and 7... prefixes)."""
    digits = _NON_DIGITS.sub("", phone)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def client_key(row: dict[str, Any]) -> str | None:
    """Stable identity for the *client*, used to find their first call.

    Prefers the linked CRM entity (``CONTACT:123`` / ``LEAD:45``); falls back to
    the normalized phone number when no CRM entity is attached.
    """
    entity_type = row.get("CRM_ENTITY_TYPE")
    entity_id = row.get("CRM_ENTITY_ID")
    if entity_type and entity_id:
        return f"{entity_type}:{entity_id}"
    phone = row.get("PHONE_NUMBER")
    if phone:
        normalized = _normalize_phone(str(phone))
        if normalized:
            return f"PHONE:{normalized}"
    return None


def parse_started_at(row: dict[str, Any]) -> datetime | None:
    """Parse CALL_START_DATE (ISO-8601 with offset) to an aware datetime."""
    raw = row.get("CALL_START_DATE")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_call_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Build the column values for upserting a :class:`Call`."""
    return {
        "bitrix_call_id": str(row["CALL_ID"]),
        "bitrix_row_id": _as_int(row.get("ID")),
        "portal_user_id": _as_int(row.get("PORTAL_USER_ID")),
        "direction": map_direction(row.get("CALL_TYPE")),
        "started_at": parse_started_at(row),
        "duration_sec": _as_int(row.get("CALL_DURATION")) or 0,
        "phone_number": (str(row["PHONE_NUMBER"]) if row.get("PHONE_NUMBER") else None),
        "crm_entity_type": row.get("CRM_ENTITY_TYPE") or None,
        "crm_entity_id": _as_int(row.get("CRM_ENTITY_ID")),
        "crm_activity_id": _as_int(row.get("CRM_ACTIVITY_ID")),
        "client_key": client_key(row),
        "recording_url": row.get("CALL_RECORD_URL") or None,
        "record_file_id": _as_int(row.get("RECORD_FILE_ID")),
    }
