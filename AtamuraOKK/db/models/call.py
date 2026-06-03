"""Call model — the pipeline work queue (one row per Bitrix call)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from AtamuraOKK.db.base import Base
from AtamuraOKK.db.models.enums import CallStatus


class Call(Base):
    """A Bitrix telephony call, tracked through the scoring pipeline."""

    __tablename__ = "calls"
    __table_args__ = (
        # FIFO queue pulls per stage, and dashboard windows by manager/time.
        Index("ix_calls_status_id", "status", "id"),
        Index("ix_calls_manager_started", "manager_id", "started_at"),
        Index("ix_calls_crm_entity", "crm_entity_type", "crm_entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    bitrix_call_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    manager_id: Mapped[int | None] = mapped_column(
        ForeignKey("managers.id"),
        nullable=True,
    )

    # CALL_TYPE: 1 = outbound, 2 = inbound.
    direction: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    duration_sec: Mapped[int] = mapped_column(Integer, default=0)
    failed_code: Mapped[str | None] = mapped_column(String(16), nullable=True)

    record_file_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    record_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    audio_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_stereo: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    crm_entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    crm_entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crm_activity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    status: Mapped[CallStatus] = mapped_column(
        String(16),
        default=CallStatus.NEW,
        index=True,
    )
    error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    failed_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
