"""Ingestion cursor state — the high-watermark for incremental pulls."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from AtamuraOKK.db.base import Base


class IngestState(Base):
    """A named cursor (keyed so multiple pullers can coexist)."""

    __tablename__ = "ingest_state"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(length=64), unique=True, index=True)
    # ISO-8601 CALL_START_DATE of the last processed call (incremental cursor).
    last_cursor: Mapped[str | None] = mapped_column(String(length=64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
