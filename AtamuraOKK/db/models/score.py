"""Score model — QA scoring output (Phase 3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from AtamuraOKK.db.base import Base


class Score(Base):
    """One QA score per call (re-scoring creates a new row; latest wins)."""

    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"),
        index=True,
    )
    rubric_version: Mapped[str | None] = mapped_column(String(length=64))
    total_score: Mapped[float | None] = mapped_column(Numeric(5, 2))
    # Per criterion, maps to score plus justification plus evidence snippet.
    criteria: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Sentiment keyed by speaker (customer and agent).
    sentiment: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    summary: Mapped[str | None] = mapped_column(Text)
    # List of red-flag tags such as rudeness or missed compliance.
    flags: Mapped[list[Any] | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(length=128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
