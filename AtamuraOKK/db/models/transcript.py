"""Transcript model (1:1 with a Call)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from AtamuraOKK.db.base import Base


class Transcript(Base):
    """The transcription of a call (speaker-tagged text + timestamped segments)."""

    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id"),
        unique=True,
        index=True,
    )
    language: Mapped[str] = mapped_column(String(16), default="auto")
    language_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    full_text: Mapped[str] = mapped_column(Text, default="")
    segments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    model: Mapped[str] = mapped_column(String(64), default="")
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
