"""Tests for the scoring worker CLI routing (call vs meeting)."""

from __future__ import annotations

from AtamuraOKK.db.models.enums import CallSource
from AtamuraOKK.scoring.__main__ import _build_service
from AtamuraOKK.settings import settings


def test_meeting_kind_routes_to_meeting_rubric_and_source() -> None:
    """--kind meeting selects op_meeting rows + the meeting rubric, no duration gate."""
    service, source = _build_service(kind="meeting")

    assert source is CallSource.OP_MEETING
    assert service._rubric_version == settings.score_meeting_rubric_version
    # Meetings carry no telephony duration -> gates relaxed to 0.
    assert service._min_duration_sec == 0
    assert service._short_contact_min_sec == 0


def test_call_kind_routes_to_call_rubric_and_source() -> None:
    """--kind call keeps bitrix_call rows, the call rubric, and call duration gates."""
    service, source = _build_service(kind="call")

    assert source is CallSource.BITRIX_CALL
    assert service._rubric_version == settings.score_rubric_version
    assert service._min_duration_sec == settings.score_min_duration_sec
    assert service._short_contact_min_sec == settings.short_contact_min_sec
