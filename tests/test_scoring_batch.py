"""Claim-release semantics for the batch scoring path.

The batch path holds calls claimed across a long round-trip, so how it *lets go*
matters more than in the realtime path: a call the model never scored has to come
back on the queue clean, while a genuinely bad one has to burn an attempt so it
eventually dead-letters. These commit through ``session_scope`` (like
``test_claim.py``) because they assert on real transitions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.scoring.batch import _fail, _heartbeat, _release

_PREFIX = "batchtest-"


async def _seed(status: CallStatus = CallStatus.SCORING, attempts: int = 0) -> int:
    async with session_scope() as session:
        call = Call(
            bitrix_call_id=f"{_PREFIX}{status.value}-{attempts}",
            analyzable=True,
            status=status,
            attempts=attempts,
        )
        session.add(call)
        await session.flush()
        return call.id


async def _get(call_id: int) -> Call:
    async with session_scope() as session:
        call = await session.get(Call, call_id)
        assert call is not None
        await session.refresh(call)
        session.expunge(call)
        return call


async def _cleanup() -> None:
    async with session_scope() as session:
        await session.execute(
            delete(Call).where(Call.bitrix_call_id.like(f"{_PREFIX}%")),
        )


@pytest.fixture
async def _seeded(_engine: AsyncEngine) -> AsyncIterator[None]:
    await _cleanup()
    try:
        yield
    finally:
        await _cleanup()


async def test_release_requeues_without_burning_an_attempt(_seeded: None) -> None:
    """A never-scored call goes back to TRANSCRIBED with its retry budget intact."""
    call_id = await _seed(attempts=1)

    await _release(call_id)

    call = await _get(call_id)
    assert call.status == CallStatus.TRANSCRIBED
    assert call.attempts == 1  # unchanged
    assert call.claimed_at is None


async def test_fail_burns_an_attempt(_seeded: None) -> None:
    """A genuinely failed call dead-letters normally via ops-retry."""
    call_id = await _seed(attempts=1)

    await _fail(call_id, "batch errored: overloaded_error")

    call = await _get(call_id)
    assert call.status == CallStatus.FAILED
    assert call.attempts == 2
    assert call.error == "batch errored: overloaded_error"


async def test_release_ignores_calls_it_no_longer_owns(_seeded: None) -> None:
    """A reclaimed or already-scored call must not be dragged back to TRANSCRIBED."""
    call_id = await _seed(status=CallStatus.SCORED)

    await _release(call_id)
    await _fail(call_id, "should not apply")

    call = await _get(call_id)
    assert call.status == CallStatus.SCORED


async def test_heartbeat_refreshes_only_claimed_calls(_seeded: None) -> None:
    """The heartbeat keeps live claims alive without touching anything else."""
    claimed = await _seed(status=CallStatus.SCORING)
    scored = await _seed(status=CallStatus.SCORED)

    await _heartbeat([claimed, scored])

    async with session_scope() as session:
        rows = {
            c.id: c
            for c in (
                await session.scalars(
                    select(Call).where(Call.id.in_([claimed, scored]))
                )
            ).all()
        }
    assert rows[claimed].claimed_at is not None
    assert rows[scored].claimed_at is None
