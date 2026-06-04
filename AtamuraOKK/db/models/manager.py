"""Manager model — a Bitrix user whose calls are scored."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from AtamuraOKK.db.base import Base


class Manager(Base):
    """A Bitrix user (PORTAL_USER_ID) mapped to a department.

    Populated from ``user.get``; may exist with only ``bitrix_user_id`` until
    the ``user`` scope is granted and the name/email/department backfilled.
    """

    __tablename__ = "managers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    bitrix_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(length=255))
    last_name: Mapped[str | None] = mapped_column(String(length=255))
    email: Mapped[str | None] = mapped_column(String(length=255))
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="SET NULL"),
        index=True,
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # False until user.get has filled in the profile (graceful degrade path).
    enriched: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
