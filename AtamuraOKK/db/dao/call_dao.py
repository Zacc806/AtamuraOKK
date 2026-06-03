"""Data access for the calls work-queue table."""

from __future__ import annotations

from typing import Any

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.dependencies import get_db_session
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus


class CallDAO:
    """Read/write access to the ``calls`` table."""

    def __init__(self, session: AsyncSession = Depends(get_db_session)) -> None:
        self.session = session

    async def get(self, call_id: int) -> Call | None:
        """Fetch a call by primary key."""
        return await self.session.get(Call, call_id)

    async def upsert_from_bitrix(self, values: dict[str, Any]) -> None:
        """Idempotent upsert keyed on ``bitrix_call_id``.

        On conflict, refresh ingestion-owned metadata but preserve ``status``
        (and thus downstream pipeline progress) for calls already in flight.
        """
        stmt = pg_insert(Call).values(**values)
        update_cols = {
            key: getattr(stmt.excluded, key)
            for key in values
            if key != "status"
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["bitrix_call_id"],
            set_=update_cols,
        )
        await self.session.execute(stmt)

    async def claim_batch(self, status: CallStatus, limit: int) -> list[Call]:
        """Lock and return up to ``limit`` calls in ``status`` (FIFO).

        Uses ``FOR UPDATE SKIP LOCKED`` so multiple worker processes can pull
        disjoint batches without contention.
        """
        stmt = (
            select(Call)
            .where(Call.status == status)
            .order_by(Call.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def mark(
        self,
        call: Call,
        status: CallStatus,
        *,
        error: str | None = None,
        failed_stage: str | None = None,
    ) -> None:
        """Set a call's status (and error/failed_stage on failure)."""
        call.status = status
        call.error = error
        call.failed_stage = failed_stage
        if status == CallStatus.FAILED:
            call.attempts += 1
