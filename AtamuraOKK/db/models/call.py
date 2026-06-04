"""Call model — one Bitrix telephony call.

Every call is stored (slim metadata) so we can determine the *first call per
client*. Only **analyzable** calls (first call AND client qualified) are
downloaded, transcribed, and scored; the rest sit in ``SKIPPED`` with a reason.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from AtamuraOKK.db.base import Base
from AtamuraOKK.db.models.enums import CallDirection, CallSource, CallStatus


class Call(Base):
    """A single call pulled from ``voximplant.statistic.get``."""

    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Recording origin: telephony call (default) or ОП face-to-face meeting.
    # Drives scorer/rubric selection and lets the dashboard exclude meetings
    # from call-volume metrics. Additive — existing rows default to a call.
    source: Mapped[CallSource] = mapped_column(
        String(length=16),
        default=CallSource.BITRIX_CALL,
        server_default=CallSource.BITRIX_CALL.value,
        index=True,
    )

    # --- Bitrix identity / cursor ---
    bitrix_call_id: Mapped[str] = mapped_column(
        String(length=255),
        unique=True,
        index=True,
    )
    # Statistic row ID — monotonic, used as the ingestion high-watermark.
    bitrix_row_id: Mapped[int | None] = mapped_column(BigInteger, index=True)

    # --- Who / when ---
    portal_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    manager_id: Mapped[int | None] = mapped_column(
        ForeignKey("managers.id", ondelete="SET NULL"),
        index=True,
    )
    direction: Mapped[CallDirection] = mapped_column(
        String(length=16),
        default=CallDirection.UNKNOWN,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )
    duration_sec: Mapped[int] = mapped_column(Integer, default=0)

    # --- Client identity (for first-call / qualification) ---
    phone_number: Mapped[str | None] = mapped_column(String(length=64), index=True)
    crm_entity_type: Mapped[str | None] = mapped_column(String(length=32))
    crm_entity_id: Mapped[int | None] = mapped_column(BigInteger)
    crm_activity_id: Mapped[int | None] = mapped_column(BigInteger)
    # Normalized client key, e.g. "CONTACT:123" or "PHONE:+7..." — the unit the
    # first-call rule groups on.
    client_key: Mapped[str | None] = mapped_column(String(length=128), index=True)

    # --- Recording ---
    recording_url: Mapped[str | None] = mapped_column(Text)
    record_file_id: Mapped[int | None] = mapped_column(BigInteger)
    audio_object_key: Mapped[str | None] = mapped_column(String(length=512))
    is_stereo: Mapped[bool | None] = mapped_column(Boolean)
    language: Mapped[str | None] = mapped_column(String(length=8))

    # --- Analysis scope flags ---
    is_first_call: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    client_qualified: Mapped[bool | None] = mapped_column(Boolean)
    analyzable: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        index=True,
    )

    # --- Lifecycle ---
    status: Mapped[CallStatus] = mapped_column(
        String(length=16),
        default=CallStatus.NEW,
        server_default=CallStatus.NEW.value,
        index=True,
    )
    skip_reason: Mapped[str | None] = mapped_column(String(length=64))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
