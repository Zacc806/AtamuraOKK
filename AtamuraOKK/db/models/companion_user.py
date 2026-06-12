"""CompanionUser model — a person allowed into the sales-companion cabinet."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from AtamuraOKK.db.base import Base
from AtamuraOKK.db.models.enums import CompanionRole


class CompanionUser(Base):
    """Cabinet login: a personal access key bound to a role.

    The key itself is never stored — only its SHA-256 hex (``key_sha256``);
    the issuer (the CLI ``python -m AtamuraOKK.companion_users`` or the
    cabinet's ``POST /api/v1/users``) sees the raw key exactly once at
    creation. ``bitrix_user_id`` links a MANAGER to their `managers` row and
    is what the API scopes their data to; HEAD users may leave it NULL.
    ``department_id`` (a **Bitrix** department id) scopes a HEAD to one
    department — an office РОП; NULL keeps the head global.
    """

    __tablename__ = "companion_users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key_sha256: Mapped[str] = mapped_column(
        String(length=64),
        unique=True,
        index=True,
    )
    role: Mapped[CompanionRole] = mapped_column(
        String(length=16),
        default=CompanionRole.MANAGER,
        server_default=CompanionRole.MANAGER.value,
    )
    bitrix_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    department_id: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str | None] = mapped_column(String(length=255))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
