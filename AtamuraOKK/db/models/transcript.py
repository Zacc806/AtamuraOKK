"""Transcript model — output of the transcription worker (Phase 2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from AtamuraOKK.db.base import Base


class Transcript(Base):
    """One transcript per call."""

    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    language: Mapped[str | None] = mapped_column(String(length=8))
    full_text: Mapped[str] = mapped_column(Text, default="")
    # Ordered speech segments, each with speaker, start, end, and text.
    segments: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    # Provider/model, e.g. "openai/gpt-4o-transcribe" or "faster-whisper/large-v3".
    model: Mapped[str | None] = mapped_column(String(length=128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
