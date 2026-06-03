"""Data access for the ingestion cursor."""

from __future__ import annotations

from datetime import datetime

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.dependencies import get_db_session
from AtamuraOKK.db.models.ingest_state import IngestState


class IngestStateDAO:
    """Read/write access to the ``ingest_state`` cursor table."""

    def __init__(self, session: AsyncSession = Depends(get_db_session)) -> None:
        self.session = session

    async def get(self, key: str) -> IngestState | None:
        """Fetch the cursor row for a source key."""
        stmt = select(IngestState).where(IngestState.key == key)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def advance(
        self,
        key: str,
        *,
        last_call_id: int,
        last_window_end: datetime | None,
    ) -> None:
        """Persist the cursor after a successful ingest run."""
        state = await self.get(key)
        if state is None:
            self.session.add(
                IngestState(
                    key=key,
                    last_call_id=last_call_id,
                    last_window_end=last_window_end,
                ),
            )
            return
        state.last_call_id = max(state.last_call_id, last_call_id)
        state.last_window_end = last_window_end
