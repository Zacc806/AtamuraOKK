"""Meeting model — one scored ОП meeting recording.

Mirrored from the meeting pipeline's SQLite state by
``AtamuraOKK/scoring/meetings/push.py``. Only **scored** meetings land here
(the pipeline's in-flight state stays in SQLite), so this table is a read
surface for the companion cabinet/Metabase, not a work queue. The meeting is
attributed to whoever uploaded the recording to the Disk folder
(``uploaded_by_bitrix_id`` → ``managers``). ``source`` distinguishes
departments as more of them start dropping recordings.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
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


class Meeting(Base):
    """A scored meeting recording from the "Встречи ОП" Disk dump."""

    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # --- Bitrix identity ---
    bitrix_file_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(length=512))
    folder_path: Mapped[str | None] = mapped_column(Text)

    # --- Attribution ---
    # Which pipeline/department produced this row ("op" = отдел продаж).
    source: Mapped[str] = mapped_column(
        String(length=32),
        default="op",
        server_default="op",
        index=True,
    )
    # Bitrix user who uploaded the recording — the manager the meeting belongs to.
    uploaded_by_bitrix_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    manager_id: Mapped[int | None] = mapped_column(
        ForeignKey("managers.id", ondelete="SET NULL"),
        index=True,
    )

    # --- When / what ---
    meeting_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str | None] = mapped_column(String(length=8))

    # --- Score (derived from the full ScoreResult in ``score``) ---
    rubric_version: Mapped[str | None] = mapped_column(String(length=64))
    score_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    passed: Mapped[bool | None] = mapped_column(Boolean)
    call_type: Mapped[str | None] = mapped_column(String(length=64))
    manager_tone: Mapped[str | None] = mapped_column(String(length=32))
    needs_human_review: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    summary: Mapped[str | None] = mapped_column(Text)
    red_flags: Mapped[list[Any] | None] = mapped_column(JSONB)
    # Full ScoreResult dict (criteria, script adherence, meta, …).
    score: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
