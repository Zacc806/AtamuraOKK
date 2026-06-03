"""Score model (a rubric evaluation of a call)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from AtamuraOKK.db.base import Base


class Score(Base):
    """One quality-control score for a call (re-scoring allowed per rubric version)."""

    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id"), index=True)
    transcript_id: Mapped[int | None] = mapped_column(
        ForeignKey("transcripts.id"),
        nullable=True,
    )
    rubric_version: Mapped[str] = mapped_column(String(64), index=True)

    total_score: Mapped[int] = mapped_column(Integer)
    max_total: Mapped[int] = mapped_column(Integer)
    score_pct: Mapped[float] = mapped_column(Float, index=True)
    passed: Mapped[bool] = mapped_column(Boolean)

    criteria: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    client_agreed_meeting: Mapped[bool] = mapped_column(Boolean, default=False)
    manager_tone: Mapped[str] = mapped_column(String(32), default="")
    red_flags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")

    language: Mapped[str] = mapped_column(String(8), default="")
    provider: Mapped[str] = mapped_column(String(16), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    needs_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
