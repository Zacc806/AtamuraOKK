"""Score model — QA scoring output (Phase 3).

Superset shape: keeps the original dashboard-contract columns (total_score,
criteria, sentiment, summary, flags, model) and adds the scoring-subsystem
fields (score_pct, passed, call_type, script_adherence, ...). One row per
scoring; latest per call wins.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
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
    rubric_version: Mapped[str | None] = mapped_column(String(length=64), index=True)

    # --- Headline KPI (0-100); total_score mirrors score_pct for the dashboard. ---
    total_score: Mapped[float | None] = mapped_column(Numeric(5, 2))
    score_pct: Mapped[float | None] = mapped_column(Float, index=True)
    max_total: Mapped[int | None] = mapped_column(Integer)
    passed: Mapped[bool | None] = mapped_column(Boolean)

    # --- Detail ---
    criteria: Mapped[Any | None] = mapped_column(JSONB)  # per-criterion list/dict
    sentiment: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    summary: Mapped[str | None] = mapped_column(Text)
    flags: Mapped[list[Any] | None] = mapped_column(JSONB)  # red flags

    # --- Scoring-subsystem fields ---
    call_type: Mapped[str | None] = mapped_column(String(length=16), index=True)
    client_agreed_meeting: Mapped[bool | None] = mapped_column(Boolean)
    manager_tone: Mapped[str | None] = mapped_column(String(length=32))
    language: Mapped[str | None] = mapped_column(String(length=8))
    provider: Mapped[str | None] = mapped_column(String(length=16))
    needs_human_review: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    script_adherence: Mapped[float | None] = mapped_column(Float)
    script_deviations: Mapped[list[Any] | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(length=128))
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
