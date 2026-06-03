"""IngestState model (ingestion cursor per source)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from AtamuraOKK.db.base import Base


class IngestState(Base):
    """Cursor for an ingestion source (e.g. ``voximplant``).

    ``voximplant.statistic.get`` ignores ORDER and returns rows ascending by ID,
    so the cursor is the max numeric CALL_ID seen plus a date window; correctness
    rests on the ``calls.bitrix_call_id`` unique-upsert, not the cursor.
    """

    __tablename__ = "ingest_state"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    last_call_id: Mapped[int] = mapped_column(BigInteger, default=0)
    last_window_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
