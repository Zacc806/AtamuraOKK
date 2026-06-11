"""Versioned QA rubric definition (Phase 3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from AtamuraOKK.db.base import Base


class RubricVersion(Base):
    """A versioned scoring rubric; one row per ``source`` is ``active``.

    ``source`` is the department axis ("tm" = calls, "op" = meetings —
    matching ``meetings.source``): each department scores against its own
    criteria, so each carries its own active rubric.
    """

    __tablename__ = "rubric_versions"
    __table_args__ = (
        UniqueConstraint("source", "version", name="uq_rubric_versions_source_version"),
        Index(
            "uq_rubric_versions_active_per_source",
            "source",
            unique=True,
            postgresql_where=text("active"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(
        String(length=32),
        default="tm",
        server_default="tm",
        index=True,
    )
    version: Mapped[str] = mapped_column(String(length=64), index=True)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
