"""«недозвон»-family close reasons: settled against Voximplant, not the call.

Like the duplicate route, the telephony route never touches the LLM and never needs a
transcript — so these tests stub no judge and assert the opposite: the judge is never
reached, a deal we hold no call for is still checked, and only an *answered* call
contradicts the close (any number of unanswered attempts supports it).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.audit import service, telephony
from AtamuraOKK.db.models.audit_verdict import AuditVerdict
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.settings import settings

pytestmark = pytest.mark.anyio

_FIELD = settings.companion_closed_reason_field
_NR = settings.audit_never_reached_reason_ids[0]  # «Хронический недозвон»
_CREATE_A = "2026-07-01T09:00:00+05:00"
_CLOSE_A = "2026-07-03T10:00:00+05:00"
_CLOSE_B = "2026-07-04T11:00:00+05:00"
_PHONE = "+77015550101"


class _FakeBitrix:
    """Serves the closed-lost scan, the phone lookup, and the Voximplant attempts."""

    def __init__(
        self,
        closed: list[dict[str, Any]],
        *,
        phones: dict[int, list[str]] | None = None,
        calls_by_phone: dict[str, list[dict[str, Any]]] | None = None,
        vox_raises: bool = False,
    ) -> None:
        self.closed = closed
        self.phones = phones or {}
        self.calls_by_phone = calls_by_phone or {}
        self.vox_raises = vox_raises

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        params = params or {}
        if method == "crm.contact.list":
            for cid in params["filter"]["ID"]:
                yield {
                    "ID": str(cid),
                    "PHONE": [{"VALUE": p} for p in self.phones.get(int(cid), [])],
                }
            return
        if method == "voximplant.statistic.get":
            if self.vox_raises:
                raise RuntimeError("Bitrix throttled")
            phone = params["FILTER"]["PHONE_NUMBER"]
            for row in self.calls_by_phone.get(phone, []):
                yield row
            return
        assert method == "crm.deal.list"
        for d in self.closed:  # the closed-lost scan
            yield d

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method == "crm.deal.fields":
            return {
                _FIELD: {
                    "items": [
                        {"ID": _NR, "VALUE": "Хронический недозвон"},
                        {"ID": "101", "VALUE": "Локация не подходит"},
                    ],
                },
            }
        if method == "user.get":  # manager enrichment (ensure_managers)
            return [{"ID": "555", "NAME": "Асель", "UF_DEPARTMENT": [250]}]
        raise AssertionError(f"unexpected call: {method}")


def _row(code: str, *, call_id: str, when: str = _CLOSE_A) -> dict[str, Any]:
    return {
        "CALL_ID": call_id,
        "CALL_FAILED_CODE": code,
        "CALL_START_DATE": when,
        "PHONE_NUMBER": _PHONE,
    }


def _closed(
    deal_id: str,
    *,
    contact: str,
    reason: str = _NR,
    created: str = _CREATE_A,
    closedate: str = _CLOSE_A,
) -> dict[str, Any]:
    return {
        "ID": deal_id,
        "TITLE": "Лиды FB | Keruen",
        "ASSIGNED_BY_ID": "555",
        "CONTACT_ID": contact,
        "DATE_CREATE": created,
        "CLOSEDATE": closedate,
        _FIELD: reason,
    }


def _no_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any call to the judge is a test failure — the telephony route must not use it."""

    async def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("the LLM judge must not be called for a «недозвон» reason")

    monkeypatch.setattr(service, "build_judge_client", object)
    monkeypatch.setattr(service, "judge_one", boom)


async def _verdict(session: AsyncSession) -> AuditVerdict:
    return (await session.execute(select(AuditVerdict))).scalar_one()


# --- audit_window: CLOSEDATE is unreliable ---------------------------------------


def test_window_uses_closedate_when_valid() -> None:
    """A CLOSEDATE after DATE_CREATE bounds the window (+1d buffer)."""
    start, end = telephony.audit_window(
        {
            "DATE_CREATE": "2026-07-01T09:00:00+05:00",
            "CLOSEDATE": "2026-07-03T09:00:00+05:00",
        }
    )
    assert start == datetime.fromisoformat("2026-06-30T09:00:00+05:00")
    assert end == datetime.fromisoformat("2026-07-04T09:00:00+05:00")


def test_window_falls_back_when_closedate_precedes_creation() -> None:
    """A CLOSEDATE before DATE_CREATE is a planned default → bounded fallback span."""
    start, end = telephony.audit_window(
        {
            "DATE_CREATE": "2026-07-15T09:00:00+05:00",
            "CLOSEDATE": "2026-07-15T03:00:00+05:00",
        }
    )
    assert start == datetime.fromisoformat("2026-07-14T09:00:00+05:00")
    assert end == datetime.fromisoformat("2026-08-14T09:00:00+05:00")


def test_window_none_without_create_date() -> None:
    """No DATE_CREATE → no window to bracket the attempts."""
    assert telephony.audit_window({"CLOSEDATE": _CLOSE_A}) is None


# --- the route -------------------------------------------------------------------


async def test_answered_call_contradicts(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closed as недозвон but the number answered → contradicted, no LLM."""
    dbsession.add(Manager(bitrix_user_id=555, name="Асель", enriched=True))
    _no_judge(monkeypatch)
    deal = _closed("8001", contact="6001")
    bx = _FakeBitrix(
        [deal],
        phones={6001: [_PHONE]},
        calls_by_phone={
            _PHONE: [
                _row("304", call_id="c1"),
                _row("200", call_id="c2"),
                _row("480", call_id="c3"),
            ]
        },
    )

    stats = await service.run_audit(dbsession, bx)

    assert stats.telephony == 1
    assert stats.judged == 0
    assert stats.no_transcript == 0  # not skipped for lack of a call
    row = await _verdict(dbsession)
    assert row.verdict == "contradicted"
    assert row.model is None  # deterministic — no LLM was used
    assert row.call_ids == []
    assert row.details["check"] == telephony.CHECK_ID
    assert row.details["answered"] == 1
    assert row.details["unanswered"] == 2
    assert row.details["answered_call_ids"] == ["c2"]
    assert "дозвон был" in (row.justification or "")
    assert stats.cursor == _CLOSE_A


async def test_only_unanswered_supports(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only unanswered attempts → the недозвон stands (supported)."""
    dbsession.add(Manager(bitrix_user_id=555, name="Асель", enriched=True))
    _no_judge(monkeypatch)
    deal = _closed("8002", contact="6002")
    bx = _FakeBitrix(
        [deal],
        phones={6002: [_PHONE]},
        calls_by_phone={_PHONE: [_row("304", call_id="c1"), _row("480", call_id="c2")]},
    )

    stats = await service.run_audit(dbsession, bx)

    assert stats.telephony == 1
    row = await _verdict(dbsession)
    assert row.verdict == "supported"
    assert row.details["answered"] == 0
    assert row.details["unanswered"] == 2


async def test_no_attempts_supports(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No telephony rows at all → supported (no answered call to contradict it)."""
    dbsession.add(Manager(bitrix_user_id=555, name="Асель", enriched=True))
    _no_judge(monkeypatch)
    deal = _closed("8003", contact="6003")
    bx = _FakeBitrix([deal], phones={6003: [_PHONE]}, calls_by_phone={})

    await service.run_audit(dbsession, bx)

    row = await _verdict(dbsession)
    assert row.verdict == "supported"
    assert row.details["attempts"] == 0


async def test_deal_without_contact_is_not_determinable(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No contact → nothing to look up; never accuse on missing data."""
    _no_judge(monkeypatch)
    deal = _closed("8004", contact="0")

    await service.run_audit(dbsession, _FakeBitrix([deal]))

    row = await _verdict(dbsession)
    assert row.verdict == "not_determinable"


async def test_contact_without_phone_is_not_determinable(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contact carrying no number → not_determinable, not a false accusation."""
    dbsession.add(Manager(bitrix_user_id=555, name="Асель", enriched=True))
    _no_judge(monkeypatch)
    deal = _closed("8005", contact="6005")
    bx = _FakeBitrix([deal], phones={6005: []})

    await service.run_audit(dbsession, bx)

    row = await _verdict(dbsession)
    assert row.verdict == "not_determinable"


async def test_bitrix_failure_is_error_and_holds_cursor(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Voximplant failure degrades to error and the cursor does not advance."""
    dbsession.add(Manager(bitrix_user_id=555, name="Асель", enriched=True))
    _no_judge(monkeypatch)
    deal = _closed("8006", contact="6006")
    bx = _FakeBitrix([deal], phones={6006: [_PHONE]}, vox_raises=True)

    stats = await service.run_audit(dbsession, bx)

    row = await _verdict(dbsession)
    assert row.verdict == "error"
    assert stats.cursor is None  # errored deal is retried next pass
