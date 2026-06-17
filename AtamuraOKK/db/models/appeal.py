"""Appeal model - a manager's request to have a call's OKK score re-checked.

A manager who disagrees with the automatic OKK score files an *appeal* against
that call. The head of their department reviews it and either rejects it (the
original score stands) or accepts it, optionally recording a corrected percent
(``override_percent``). An accepted override is what the companion read layer
prefers over the LLM percent everywhere a score is shown (feed / scorecard /
team rollup); see ``web.api.v1.service._score_overrides``.

This is the second writable surface on AtamuraOKK's own Postgres (after
``companion_users``); it never touches the pipeline state or Bitrix.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from AtamuraOKK.db.base import Base

#: Appeal lifecycle. ``pending`` until a head reviews it, then a terminal verdict.
APPEAL_PENDING = "pending"
APPEAL_ACCEPTED = "accepted"
APPEAL_REJECTED = "rejected"


class Appeal(Base):
    """One manager appeal against a call's OKK score, holding the head's verdict."""

    __tablename__ = "appeals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"),
        index=True,
    )
    # The manager whose score is appealed (the call's owner). Their Bitrix user
    # id, mirroring how the rest of the companion API scopes a manager's data.
    manager_bitrix_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # Who actually filed it - normally the same manager, kept distinct for audit.
    created_by_bitrix_user_id: Mapped[int] = mapped_column(BigInteger)
    # The manager's Bitrix department id at filing time, so a scoped office head
    # can list only their own department's appeals without a join.
    department_bitrix_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(length=16),
        default=APPEAL_PENDING,
        server_default=APPEAL_PENDING,
        index=True,
    )
    # The head's corrected 0-100 percent. NULL even on an accepted appeal means
    # "reviewed, no numeric correction"; a non-NULL value on an accepted appeal
    # overrides the LLM percent in the read layer.
    override_percent: Mapped[float | None] = mapped_column(Numeric(5, 2))
    head_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by_bitrix_user_id: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
