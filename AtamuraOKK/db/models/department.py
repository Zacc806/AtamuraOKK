"""Department model — the org unit a manager belongs to."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from AtamuraOKK.db.base import Base


class Department(Base):
    """A department; a department head sees only their department's calls."""

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Bitrix department id (from user.UF_DEPARTMENT); nullable until mapped.
    bitrix_id: Mapped[int | None] = mapped_column(
        BigInteger,
        unique=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(length=255))
    # Bitrix user id of the department head (dashboard row-level access).
    head_user_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
