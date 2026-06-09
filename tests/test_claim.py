"""Concurrency tests for race-safe work-claiming (dispatch.claim).

These exercise the real ``FOR UPDATE SKIP LOCKED`` path, so they commit rows via
``session_scope`` (the production session factory) rather than the rolled-back
``dbsession`` fixture, and clean up after themselves. They depend on ``_engine``
only to guarantee the test database + schema exist.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.dispatch.claim import claim_ready, reclaim_stale

_PREFIX = "claimtest-"


async def _seed(n: int, status: CallStatus, tag: str = "a") -> list[int]:
    """Commit ``n`` analyzable calls in ``status``; return their ids."""
    async with session_scope() as session:
        calls = [
            Call(bitrix_call_id=f"{_PREFIX}{tag}-{i}", analyzable=True, status=status)
            for i in range(n)
        ]
        session.add_all(calls)
        await session.flush()
        return [c.id for c in calls]


async def _statuses(ids: list[int]) -> dict[int, CallStatus]:
    async with session_scope() as session:
        rows = (await session.scalars(select(Call).where(Call.id.in_(ids)))).all()
        return {c.id: c.status for c in rows}


async def _cleanup() -> None:
    async with session_scope() as session:
        await session.execute(
            delete(Call).where(Call.bitrix_call_id.like(f"{_PREFIX}%")),
        )


@pytest.fixture
async def _seeded(_engine: AsyncEngine) -> AsyncIterator[None]:
    """Ensure schema exists and the claimtest rows are cleaned up afterwards."""
    await _cleanup()
    try:
        yield
    finally:
        await _cleanup()


async def test_concurrent_claims_are_disjoint(_seeded: None) -> None:
    """Two concurrent claimers split the ready rows with no overlap."""
    ids = await _seed(6, CallStatus.NEW)

    first, second = await asyncio.gather(
        claim_ready(CallStatus.NEW, CallStatus.DOWNLOADING, 3),
        claim_ready(CallStatus.NEW, CallStatus.DOWNLOADING, 3),
    )

    assert set(first).isdisjoint(second)
    assert sorted(first + second) == sorted(ids)
    statuses = await _statuses(ids)
    assert all(s == CallStatus.DOWNLOADING for s in statuses.values())


async def test_claim_sets_claimed_at(_seeded: None) -> None:
    """A claimed row is flipped to in-flight with claimed_at stamped."""
    ids = await _seed(2, CallStatus.NEW)

    claimed = await claim_ready(CallStatus.NEW, CallStatus.DOWNLOADING, 10)

    assert sorted(claimed) == sorted(ids)
    async with session_scope() as session:
        rows = (await session.scalars(select(Call).where(Call.id.in_(ids)))).all()
        assert all(c.status == CallStatus.DOWNLOADING for c in rows)
        assert all(c.claimed_at is not None for c in rows)


async def test_reclaim_stale_reverts_old_claims(_seeded: None) -> None:
    """A claim older than the TTL reverts to its ready status; fresh ones don't."""
    [stale_id] = await _seed(1, CallStatus.TRANSCRIBING, tag="stale")
    [fresh_id] = await _seed(1, CallStatus.TRANSCRIBING, tag="fresh")
    # Backdate one claim well past any TTL; keep the other recent.
    async with session_scope() as session:
        old = await session.get(Call, stale_id)
        assert old is not None
        old.claimed_at = datetime.now(UTC) - timedelta(hours=2)
        new = await session.get(Call, fresh_id)
        assert new is not None
        new.claimed_at = datetime.now(UTC)

    reclaimed = await reclaim_stale(
        CallStatus.TRANSCRIBING,
        CallStatus.DOWNLOADED,
        ttl_seconds=600,
    )

    assert reclaimed == 1
    statuses = await _statuses([stale_id, fresh_id])
    assert statuses[stale_id] == CallStatus.DOWNLOADED
    assert statuses[fresh_id] == CallStatus.TRANSCRIBING
