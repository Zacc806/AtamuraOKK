"""Appeal model - a manager's request to have a call's OKK score re-checked.

A manager who disagrees with the automatic OKK score files an *appeal* against
that call, listing the specific rubric **criteria** they contest
(``disputed_criteria``). The head of their department listens to the recording
and confirms the subset of those criteria the manager was right about
(``confirmed_criteria``). Each confirmed criterion is awarded full marks and the
call's total is recomputed from the per-criterion payload; that corrected
percent is stored in ``override_percent`` (computed, never hand-typed). An
accepted override is what the companion read layer prefers over the LLM percent
everywhere a score is shown (feed / scorecard / team rollup); see
``web.api.v1.service._score_overrides``. The official QA reports keep the LLM
verdict — the override lives only in the cabinet read layer.

This is the second writable surface on AtamuraOKK's own Postgres (after
``companion_users``); it never touches the pipeline state or Bitrix.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
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
    # Legacy: a single contested block_name. Superseded by disputed_criteria;
    # kept nullable so pre-existing appeals still read. New appeals leave it NULL.
    disputed_block: Mapped[str | None] = mapped_column(String(length=255))
    # The specific rubric criteria the manager contests, as a JSONB list of
    # ``{"criterion_id": int, "reason": str | None}`` (criterion_id is the
    # rubric criterion number from the call's per_criterion payload). Empty list
    # means a general appeal with no per-criterion correction to apply.
    disputed_criteria: Mapped[list[Any] | None] = mapped_column(JSONB)
    # The head's verdict: a JSONB list of criterion_ids (subset of
    # disputed_criteria) confirmed in the manager's favour and awarded full
    # marks. Drives the recomputed override_percent. NULL/empty = none confirmed.
    confirmed_criteria: Mapped[list[Any] | None] = mapped_column(JSONB)
    # Red flags the head cleared when accepting the appeal, as a JSONB list of
    # the flag strings (matched against the call's ``Score.flags``). Lets a
    # presentation red flag disappear once its criterion is upheld, so the
    # corrected breakdown stays self-consistent. Only applied on an accepted
    # appeal; the read layer hides these flags (QA reports keep them).
    dismissed_flags: Mapped[list[Any] | None] = mapped_column(JSONB)
    # The manager's own feedback on the call - why they disagree.
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(length=16),
        default=APPEAL_PENDING,
        server_default=APPEAL_PENDING,
        index=True,
    )
    # The recomputed 0-100 percent after awarding full marks to confirmed_criteria
    # (computed at review time, not hand-typed). NULL even on an accepted appeal
    # means "reviewed, no criterion confirmed"; a non-NULL value overrides the LLM
    # percent in the read layer.
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
