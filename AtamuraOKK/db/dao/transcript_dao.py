"""Data access for the transcripts table."""

from __future__ import annotations

from typing import Any

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.dependencies import get_db_session
from AtamuraOKK.db.models.transcript import Transcript


class TranscriptDAO:
    """Read/write access to the ``transcripts`` table."""

    def __init__(self, session: AsyncSession = Depends(get_db_session)) -> None:
        self.session = session

    async def get_by_call(self, call_id: int) -> Transcript | None:
        """Fetch the transcript for a call, or None."""
        stmt = select(Transcript).where(Transcript.call_id == call_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        call_id: int,
        language: str,
        full_text: str,
        segments: list[dict[str, Any]],
        model: str,
        language_probability: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Transcript:
        """Create and persist a transcript row."""
        transcript = Transcript(
            call_id=call_id,
            language=language,
            language_probability=language_probability,
            full_text=full_text,
            segments=segments,
            model=model,
            meta=meta or {},
        )
        self.session.add(transcript)
        await self.session.flush()
        return transcript
