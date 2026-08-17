"""Batched close-reason judging: submit, park, settle — and never pay twice.

The judge route is the only part of the audit that costs tokens, and its verdicts feed a
queue nobody watches in realtime, so it goes through the Message Batches API at half
price. That trades one property away: a verdict no longer lands in the pass that found
the deal. These tests pin down what has to hold across that gap — the deal is parked
(not done, so the cursor waits), it is not submitted a second time, a landed batch fills
in the real verdict, and every way a batch can fail to produce one puts the deal back in
scope instead of leaving it parked forever.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from AtamuraOKK.audit import batch, service
from AtamuraOKK.db.models.audit_batch import AuditBatch
from AtamuraOKK.db.models.audit_verdict import AuditVerdict
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.settings import settings

pytestmark = pytest.mark.anyio

_FIELD = settings.companion_closed_reason_field
_CLOSE_A = "2026-07-01T10:00:00+05:00"
_CLOSE_B = "2026-07-02T11:00:00+05:00"
_BATCH_ID = "msgbatch_01test"


class _FakeBitrix:
    """Replays the closed-lost scan, the reason labels, and the card-note reads."""

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
        for d in self.deals:
            yield d

    async def batch(self, commands: dict[str, Any]) -> dict[str, Any]:
        return {key: [] for key in commands}  # no card notes in these fixtures

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        assert method == "crm.deal.fields"
        return {_FIELD: {"items": [{"ID": "101", "VALUE": "Локация не подходит"}]}}


class _Result:
    """One entry of a batch results stream."""

    def __init__(self, custom_id: str, result: Any) -> None:
        self.custom_id = custom_id
        self.result = result


class _Succeeded:
    def __init__(self, message: Any) -> None:
        self.type = "succeeded"
        self.message = message


class _Failed:
    def __init__(self, kind: str = "expired") -> None:
        self.type = kind


class _Message:
    """A judge response: the forced tool call, or a truncated one."""

    def __init__(self, verdict: str, *, stop_reason: str = "tool_use") -> None:
        self.stop_reason = stop_reason
        self.content = [
            type(
                "Block",
                (),
                {
                    "type": "tool_use",
                    "name": "record_reason_verdict",
                    "input": {
                        "verdict": verdict,
                        "confidence": 0.9,
                        "justification": "по разговору причина не подтверждается",
                        "evidence_quote": "мне интересно",
                    },
                },
            )()
        ]


class _FakeBatches:
    def __init__(self, owner: _FakeAnthropic) -> None:
        self._owner = owner

    async def create(self, *, requests: list[Any]) -> Any:
        if self._owner.create_raises:
            raise RuntimeError("credit balance is too low")
        self._owner.submitted.append(requests)
        return type("Batch", (), {"id": self._owner.batch_id})()

    async def retrieve(self, provider_id: str) -> Any:
        self._owner.retrieved.append(provider_id)
        return type(
            "Batch",
            (),
            {
                "processing_status": self._owner.status,
                "ended_at": datetime(2026, 7, 2, tzinfo=UTC),
            },
        )()

    async def results(self, provider_id: str) -> AsyncIterator[_Result]:
        async def gen() -> AsyncIterator[_Result]:
            for entry in self._owner.results_by_batch.get(provider_id, []):
                yield entry

        return gen()


class _FakeAnthropic:
    """Stands in for AsyncAnthropic's batches surface."""

    def __init__(
        self,
        *,
        status: str = "ended",
        results: dict[str, list[_Result]] | None = None,
        create_raises: bool = False,
        batch_id: str = _BATCH_ID,
    ) -> None:
        self.status = status
        self.results_by_batch = results or {}
        self.create_raises = create_raises
        self.batch_id = batch_id
        self.submitted: list[list[Any]] = []
        self.retrieved: list[str] = []
        self.messages = type("Messages", (), {"batches": _FakeBatches(self)})()


def _deal(deal_id: str, *, contact: str, closedate: str = _CLOSE_A) -> dict[str, Any]:
    return {
        "ID": deal_id,
        "TITLE": f"Клиент {deal_id}",
        "ASSIGNED_BY_ID": "555",
        "CONTACT_ID": contact,
        "CLOSEDATE": closedate,
        _FIELD: "101",
    }


async def _seed_client(session: AsyncSession, contact_id: int) -> None:
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


def _use(monkeypatch: pytest.MonkeyPatch, client: _FakeAnthropic) -> None:
    """Point both the submit and the poll path at the fake client."""
    monkeypatch.setattr(settings, "audit_judge_batch_enabled", True)
    monkeypatch.setattr(batch, "_client", lambda: client)


async def _rows(session: AsyncSession) -> list[AuditVerdict]:
    return list((await session.execute(select(AuditVerdict))).scalars().all())


async def _batches(session: AsyncSession) -> list[AuditBatch]:
    return list((await session.execute(select(AuditBatch))).scalars().all())


# --- submit ----------------------------------------------------------------------


async def test_submit_parks_the_deal_and_holds_the_cursor(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submitted deal gets a pending row, and the cursor must not move past it.

    If the cursor advanced on submit, a batch that never came back would take the deal
    with it — the next pass would start after it and never look at it again.
    """
    dbsession.add(Manager(bitrix_user_id=555, enriched=True))
    await _seed_client(dbsession, 5001)
    client = _FakeAnthropic()
    _use(monkeypatch, client)

    stats = await service.run_audit(
        dbsession, _FakeBitrix([_deal("7001", contact="5001")])
    )

    assert stats.submitted == 1
    assert stats.judged == 0
    assert stats.cursor is None  # parked, not done
    assert len(client.submitted) == 1
    row = (await _rows(dbsession))[0]
    assert row.bitrix_deal_id == 7001
    assert row.verdict == batch.PENDING_VERDICT
    assert row.close_reason == "Локация не подходит"  # context kept for the poller
    assert row.manager_id is not None
    assert row.model == settings.anthropic_scoring_model
    open_batch = (await _batches(dbsession))[0]
    assert open_batch.provider_batch_id == _BATCH_ID
    assert open_batch.deal_ids == [7001]
    assert open_batch.status == "in_flight"


async def test_pending_deal_is_not_submitted_twice(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit runs every 30 min; an in-flight deal must not be billed twice."""
    dbsession.add(Manager(bitrix_user_id=555, enriched=True))
    await _seed_client(dbsession, 5001)
    client = _FakeAnthropic()
    _use(monkeypatch, client)
    deals = [_deal("7001", contact="5001")]

    await service.run_audit(dbsession, _FakeBitrix(deals))
    second = await service.run_audit(dbsession, _FakeBitrix(deals))

    assert len(client.submitted) == 1  # not re-submitted
    assert second.in_flight == 1
    assert second.submitted == 0
    assert second.cursor is None  # still parked
    assert len(await _rows(dbsession)) == 1


async def test_a_pending_deal_is_invisible_to_the_cabinet(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Отказы не по делу» must not show a deal whose verdict has not been decided."""
    from AtamuraOKK.web.api.v1 import day

    dbsession.add(Manager(bitrix_user_id=555, enriched=True))
    await _seed_client(dbsession, 5001)
    _use(monkeypatch, _FakeAnthropic())
    await service.run_audit(dbsession, _FakeBitrix([_deal("7001", contact="5001")]))

    items = await day._audit_failed_items(dbsession, 555, 20)

    assert items == []


async def test_rejected_submit_leaves_the_deal_in_scope(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero credits must not park a deal: no pending row, no batch row, retried next."""
    dbsession.add(Manager(bitrix_user_id=555, enriched=True))
    await _seed_client(dbsession, 5001)
    _use(monkeypatch, _FakeAnthropic(create_raises=True))

    stats = await service.run_audit(
        dbsession, _FakeBitrix([_deal("7001", contact="5001")])
    )

    assert stats.submitted == 0
    assert await dbsession.scalar(select(func.count()).select_from(AuditVerdict)) == 0
    assert await dbsession.scalar(select(func.count()).select_from(AuditBatch)) == 0
    assert stats.cursor is None  # nothing settled → cursor holds


async def test_settled_row_releases_the_cursor(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parking is only safe if settling actually un-parks: cursor moves once decided.

    The verdict is written straight onto the row here rather than through the poller —
    what is under test is the *cursor* rule (pending holds, decided releases); the
    poller's own transitions have their own tests below.
    """
    dbsession.add(Manager(bitrix_user_id=555, enriched=True))
    await _seed_client(dbsession, 5001)
    _use(monkeypatch, _FakeAnthropic())
    deals = [_deal("7001", contact="5001")]
    first = await service.run_audit(dbsession, _FakeBitrix(deals))
    assert first.cursor is None

    row = (await _rows(dbsession))[0]
    row.verdict = "supported"
    await dbsession.flush()
    second = await service.run_audit(dbsession, _FakeBitrix(deals))

    assert second.already_done == 1
    assert second.in_flight == 0
    assert second.cursor == _CLOSE_A


# --- poll ------------------------------------------------------------------------
#
# The poller commits through its own `session_scope`, so — as in
# tests/test_scoring_batch.py — these use real transactions and clean up after
# themselves instead of the rolled-back `dbsession` fixture, whose writes another
# connection cannot see.

_POLL_DEAL = 990001
_POLL_DEAL_B = 990002


async def _seed_in_flight(*deal_ids: int) -> None:
    """A pending verdict per deal plus the in-flight batch covering them."""
    async with session_scope() as session:
        for deal_id in deal_ids:
            session.add(
                AuditVerdict(
                    bitrix_deal_id=deal_id,
                    deal_title=f"Клиент {deal_id}",
                    close_reason="Локация не подходит",
                    reason_id="101",
                    verdict=batch.PENDING_VERDICT,
                    call_ids=[],
                    details={"check": "llm_judge/v1", "notes": []},
                    model=settings.anthropic_scoring_model,
                ),
            )
        session.add(
            AuditBatch(
                provider_batch_id=_BATCH_ID,
                status="in_flight",
                deal_ids=list(deal_ids),
                model=settings.anthropic_scoring_model,
            ),
        )


async def _poll_cleanup() -> None:
    async with session_scope() as session:
        await session.execute(
            delete(AuditVerdict).where(
                AuditVerdict.bitrix_deal_id.in_([_POLL_DEAL, _POLL_DEAL_B]),
            ),
        )
        await session.execute(
            delete(AuditBatch).where(AuditBatch.provider_batch_id == _BATCH_ID),
        )


@pytest.fixture
async def _poll_state(_engine: AsyncEngine) -> AsyncIterator[None]:
    await _poll_cleanup()
    try:
        yield
    finally:
        await _poll_cleanup()


async def _fetch(deal_id: int) -> AuditVerdict | None:
    async with session_scope() as session:
        row = await session.scalar(
            select(AuditVerdict).where(AuditVerdict.bitrix_deal_id == deal_id),
        )
        if row is not None:
            session.expunge(row)
        return row


async def test_poll_settles_the_pending_row(
    _poll_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A landed batch turns the pending row into the real verdict, in place."""
    await _seed_in_flight(_POLL_DEAL)
    _use(
        monkeypatch,
        _FakeAnthropic(
            results={
                _BATCH_ID: [
                    _Result(f"deal-{_POLL_DEAL}", _Succeeded(_Message("contradicted"))),
                ],
            },
        ),
    )

    stats = await batch.poll_batches()

    assert stats.ended == 1
    assert stats.settled == 1
    assert stats.verdicts == {"contradicted": 1}
    row = await _fetch(_POLL_DEAL)
    assert row is not None
    assert row.verdict == "contradicted"
    assert row.confidence == 0.9
    assert "не подтверждается" in (row.justification or "")
    assert row.evidence_quote == "мне интересно"
    assert row.close_reason == "Локация не подходит"  # context preserved
    async with session_scope() as session:
        done = await session.scalar(
            select(AuditBatch).where(AuditBatch.provider_batch_id == _BATCH_ID),
        )
        assert done is not None
        assert done.status == "done"
        assert done.succeeded == 1


async def test_expired_result_requeues_the_deal(
    _poll_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canceled/expired request was never judged — drop the row so it goes again.

    Marking it ``error`` would also be retried, but it would show up in the verdict
    tallies as a judgment failure, which a batch that expired is not.
    """
    await _seed_in_flight(_POLL_DEAL)
    _use(
        monkeypatch,
        _FakeAnthropic(
            results={_BATCH_ID: [_Result(f"deal-{_POLL_DEAL}", _Failed())]},
        ),
    )

    stats = await batch.poll_batches()

    assert stats.settled == 0
    assert stats.failed == 1
    assert await _fetch(_POLL_DEAL) is None  # back in scope for the next pass


async def test_truncated_verdict_requeues_the_deal(
    _poll_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forced tool call cut off at max_tokens is incomplete JSON, not a verdict."""
    await _seed_in_flight(_POLL_DEAL)
    _use(
        monkeypatch,
        _FakeAnthropic(
            results={
                _BATCH_ID: [
                    _Result(
                        f"deal-{_POLL_DEAL}",
                        _Succeeded(_Message("contradicted", stop_reason="max_tokens")),
                    ),
                ],
            },
        ),
    )

    stats = await batch.poll_batches()

    assert stats.failed == 1
    assert await _fetch(_POLL_DEAL) is None


async def test_unreported_deal_is_not_left_parked(
    _poll_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deal the batch never mentions would otherwise stay pending forever."""
    await _seed_in_flight(_POLL_DEAL, _POLL_DEAL_B)
    _use(
        monkeypatch,
        _FakeAnthropic(
            results={
                _BATCH_ID: [
                    _Result(f"deal-{_POLL_DEAL}", _Succeeded(_Message("supported"))),
                ],
            },
        ),
    )

    stats = await batch.poll_batches()

    assert stats.settled == 1
    assert stats.failed == 1  # the unreported one
    settled = await _fetch(_POLL_DEAL)
    assert settled is not None
    assert settled.verdict == "supported"
    assert await _fetch(_POLL_DEAL_B) is None


async def test_open_batch_is_left_alone_until_it_ends(
    _poll_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Polling an in-progress batch must not touch its rows."""
    await _seed_in_flight(_POLL_DEAL)
    _use(monkeypatch, _FakeAnthropic(status="in_progress"))

    stats = await batch.poll_batches()

    assert stats.open_batches == 1
    assert stats.ended == 0
    row = await _fetch(_POLL_DEAL)
    assert row is not None
    assert row.verdict == batch.PENDING_VERDICT
    async with session_scope() as session:
        still_open = await session.scalar(
            select(AuditBatch).where(AuditBatch.provider_batch_id == _BATCH_ID),
        )
        assert still_open is not None
        assert still_open.status == "in_flight"


async def test_poll_is_a_noop_with_no_open_batches(_poll_state: None) -> None:
    """The cron fires every 30 min — an idle poll must not need the API at all."""
    stats = await batch.poll_batches()

    assert stats.open_batches == 0
    assert stats.ended == 0
