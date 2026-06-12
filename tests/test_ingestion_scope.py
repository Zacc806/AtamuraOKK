"""Analysis-scope rule: every >=90s call until the client qualifies.

Unit tests over _apply_scope with in-memory Call rows: before/after the
qualification moment, unknown qualification (in scope by operator decision),
the forward-only freeze of pre-rule skip verdicts, and the in-flight guard.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.ingestion.qualification import UNKNOWN_QUALIFICATION, Qualification
from AtamuraOKK.ingestion.service import IngestStats, _apply_scope
from AtamuraOKK.settings import settings

_QUAL_AT = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
_QUALIFIED = Qualification(qualified=True, at=_QUAL_AT)


def _call(
    *,
    started_at: datetime,
    status: CallStatus = CallStatus.NEW,
    skip_reason: str | None = None,
    duration_sec: int = 120,
    is_first_call: bool = False,
) -> Call:
    return Call(
        bitrix_call_id="x",
        started_at=started_at,
        duration_sec=duration_sec,
        status=status,
        skip_reason=skip_reason,
        is_first_call=is_first_call,
    )


def test_call_before_qualification_is_analyzable() -> None:
    """Any call up to the qualification moment is in scope — first or not."""
    call = _call(started_at=datetime(2026, 6, 10, 11, 0, tzinfo=UTC))
    _apply_scope(call, _QUALIFIED, IngestStats())
    assert call.analyzable is True
    assert call.status is CallStatus.NEW
    assert call.skip_reason is None


def test_call_after_qualification_is_skipped() -> None:
    """Past the qualification moment a call is logistics -> skipped."""
    call = _call(started_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC))
    _apply_scope(call, _QUALIFIED, IngestStats())
    assert call.analyzable is False
    assert call.status is CallStatus.SKIPPED
    assert call.skip_reason == "after_qualification"


def test_never_or_unknown_qualified_is_in_scope() -> None:
    """The until-condition never fires -> every call stays analyzable."""
    for qual in (Qualification(qualified=False), UNKNOWN_QUALIFICATION):
        call = _call(started_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC))
        _apply_scope(call, qual, IngestStats())
        assert call.analyzable is True, qual
        assert call.status is CallStatus.NEW


def test_legacy_skip_verdicts_are_frozen() -> None:
    """Forward-only: rows skipped by the old rule are never reopened."""
    for reason in ("not_first_call", "not_qualified", "qualification_unknown"):
        call = _call(
            started_at=datetime(2026, 6, 10, 11, 0, tzinfo=UTC),
            status=CallStatus.SKIPPED,
            skip_reason=reason,
        )
        _apply_scope(call, _QUALIFIED, IngestStats())
        assert call.status is CallStatus.SKIPPED, reason
        assert call.skip_reason == reason


def test_after_qualification_verdict_is_recomputable() -> None:
    """The new rule's own skip is not frozen — scope can be re-derived."""
    call = _call(
        started_at=datetime(2026, 6, 10, 11, 0, tzinfo=UTC),
        status=CallStatus.SKIPPED,
        skip_reason="after_qualification",
    )
    _apply_scope(call, Qualification(qualified=False), IngestStats())
    assert call.status is CallStatus.NEW
    assert call.skip_reason is None


def test_too_short_still_skips() -> None:
    """The duration gate runs before the qualification gate."""
    call = _call(
        started_at=datetime(2026, 6, 10, 11, 0, tzinfo=UTC),
        duration_sec=30,
    )
    _apply_scope(call, _QUALIFIED, IngestStats())
    assert call.skip_reason == "too_short"


def test_in_flight_calls_are_untouched() -> None:
    """A call past NEW is never demoted by a scope recompute."""
    call = _call(
        started_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC),
        status=CallStatus.SCORED,
    )
    _apply_scope(call, _QUALIFIED, IngestStats())
    assert call.status is CallStatus.SCORED


def test_gate_disabled_by_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """ingest_until_qualified=False turns the qualification gate off."""
    monkeypatch.setattr(settings, "ingest_until_qualified", False)
    call = _call(started_at=datetime(2026, 6, 10, 13, 0, tzinfo=UTC))
    _apply_scope(call, _QUALIFIED, IngestStats())
    assert call.analyzable is True
