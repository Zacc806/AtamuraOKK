"""Close-reason audit pass: persistence, attribution, cursor and idempotency.

The LLM judge is stubbed (Anthropic credits are out and the network is off-limits
in tests); we exercise the deal→transcript join, manager attribution, the
idempotent upsert, and the contiguous-done cursor advance (an ``error`` verdict
must be retried, not skipped).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.audit import service
from AtamuraOKK.db.models.audit_verdict import AuditVerdict
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.settings import settings

pytestmark = pytest.mark.anyio

_FIELD = settings.companion_closed_reason_field
_CLOSE_A = "2026-07-01T10:00:00+05:00"
_CLOSE_B = "2026-07-02T11:00:00+05:00"


class _FakeBitrix:
    """Replays crm.deal.list (the closed-lost deals) and crm.deal.fields (labels)."""

    def __init__(self, deals: list[dict[str, Any]]) -> None:
        self.deals = deals

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        assert method == "crm.deal.list"
        for n, d in enumerate(self.deals, start=1):
            yield d
            if max_items is not None and n >= max_items:
                return

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        assert method == "crm.deal.fields"
        return {_FIELD: {"items": [{"ID": "101", "VALUE": "Локация не подходит"}]}}


def _deal(
    deal_id: str, *, contact: str | None, closedate: str, reason: str = "101"
) -> dict[str, Any]:
    return {
        "ID": deal_id,
        "TITLE": f"Клиент {deal_id}",
        "ASSIGNED_BY_ID": "555",
        "CONTACT_ID": contact,
        "CLOSEDATE": closedate,
        _FIELD: reason,
    }


async def _seed_client(session: AsyncSession, contact_id: int) -> None:
    """A scored call + transcript for CONTACT:{id}, so the deal resolves to text."""
    call = Call(
        bitrix_call_id=f"audit-{contact_id}",
        client_key=f"CONTACT:{contact_id}",
        started_at=datetime(2026, 6, 30, 9, 0, tzinfo=UTC),
        status=CallStatus.SCORED,
    )
    session.add(call)
    await session.flush()
    session.add(Transcript(call_id=call.id, full_text="[AGENT]\nздравствуйте"))
    await session.flush()


def _stub_judge(monkeypatch: pytest.MonkeyPatch, verdict: str) -> list[int]:
    """Replace the judge with a canned verdict; return a call-count list."""
    calls: list[int] = []

    async def fake_judge(client: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        return {
            "verdict": verdict,
            "confidence": 0.9,
            "justification": "клиент говорил другое",
            "evidence_quote": "мне интересно",
        }

    monkeypatch.setattr(service, "build_judge_client", object)
    monkeypatch.setattr(service, "judge_one", fake_judge)
    return calls


async def _count(session: AsyncSession) -> int:
    total = await session.scalar(select(func.count()).select_from(AuditVerdict))
    return int(total or 0)


async def test_run_audit_persists_attributes_and_advances(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A closed-lost deal with a transcript is judged, attributed, and cursored."""
    dbsession.add(Manager(bitrix_user_id=555, name="Асель", enriched=True))
    await _seed_client(dbsession, 5001)
    _stub_judge(monkeypatch, "contradicted")

    stats = await service.run_audit(
        dbsession, _FakeBitrix([_deal("7001", contact="5001", closedate=_CLOSE_A)])
    )

    assert stats.judged == 1
    row = (await dbsession.execute(select(AuditVerdict))).scalar_one()
    assert row.bitrix_deal_id == 7001
    assert row.verdict == "contradicted"
    assert row.close_reason == "Локация не подходит"
    assert row.manager_id is not None  # attributed to the ASSIGNED_BY_ID manager
    assert row.closed_at is not None
    assert stats.cursor == _CLOSE_A  # advanced to the deal's CLOSEDATE


async def test_run_audit_skips_deal_without_transcript(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deal we hold no call for is not judged, but the cursor still moves past it."""
    calls = _stub_judge(monkeypatch, "contradicted")

    stats = await service.run_audit(
        dbsession, _FakeBitrix([_deal("7002", contact="9999", closedate=_CLOSE_A)])
    )

    assert stats.no_transcript == 1
    assert stats.judged == 0
    assert not calls  # judge never called
    assert await _count(dbsession) == 0
    assert stats.cursor == _CLOSE_A  # skipped-done deal advances the cursor


async def test_run_audit_is_idempotent(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second pass over the same deal upserts (no dup) and does not re-judge."""
    dbsession.add(Manager(bitrix_user_id=555, enriched=True))
    await _seed_client(dbsession, 5001)
    calls = _stub_judge(monkeypatch, "contradicted")
    deals = [_deal("7001", contact="5001", closedate=_CLOSE_A)]

    await service.run_audit(dbsession, _FakeBitrix(deals))
    second = await service.run_audit(dbsession, _FakeBitrix(deals))

    assert await _count(dbsession) == 1  # upsert, not a duplicate
    assert len(calls) == 1  # judged once; the re-run skipped the done deal
    assert second.already_done == 1


async def test_error_verdict_holds_cursor_for_retry(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error verdict persists but does not advance the cursor (so it retries)."""
    dbsession.add(Manager(bitrix_user_id=555, enriched=True))
    await _seed_client(dbsession, 5001)
    await _seed_client(dbsession, 5002)
    _stub_judge(monkeypatch, "error")

    stats = await service.run_audit(
        dbsession,
        _FakeBitrix(
            [
                _deal("7001", contact="5001", closedate=_CLOSE_A),
                _deal("7002", contact="5002", closedate=_CLOSE_B),
            ]
        ),
    )

    assert stats.verdicts.get("error") == 2
    assert stats.cursor is None  # the leading deal errored → cursor never advanced


async def test_audit_failed_items_scopes_to_manager_and_contradicted(
    dbsession: AsyncSession,
) -> None:
    """«Мой день» shows only the manager's OWN contradicted verdicts."""
    from AtamuraOKK.web.api.v1 import day

    m1 = Manager(bitrix_user_id=555, enriched=True)
    m2 = Manager(bitrix_user_id=777, enriched=True)
    dbsession.add_all([m1, m2])
    await dbsession.flush()
    dbsession.add_all(
        [
            AuditVerdict(
                bitrix_deal_id=1,
                manager_id=m1.id,
                verdict="contradicted",
                close_reason="Локация",
                deal_title="Иван",
            ),
            AuditVerdict(
                bitrix_deal_id=2,
                manager_id=m1.id,
                verdict="supported",
                close_reason="Y",
            ),
            AuditVerdict(
                bitrix_deal_id=3,
                manager_id=m2.id,
                verdict="contradicted",
                close_reason="Z",
            ),
        ]
    )
    await dbsession.flush()

    items = await day._audit_failed_items(dbsession, 555, 20)
    assert [i.deal_id for i in items] == [1]  # only contradicted, only m1's
    assert items[0].client_name == "Иван"

    other = await day._audit_failed_items(dbsession, 777, 20)
    assert [i.deal_id for i in other] == [3]
