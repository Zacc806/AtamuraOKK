"""Close-reason audit verdict — one row per closed-lost deal we could check.

For a deal that closed as lost, the manager picks a reason from the «Причина
закрытия/отказа» Bitrix enum. The audit pass (``AtamuraOKK/audit/``) joins that
stated reason against the client's actual call transcript(s) and asks the LLM
whether the call ``supported`` / ``contradicted`` / ``not_determinable`` the
reason. The «Дубль…» reasons are settled against the CRM instead of the call
(``audit/duplicates.py``) — same verdict vocabulary, but ``model`` is NULL and the
duplicate evidence lands in ``details``. Re-auditing a deal upserts this row
(unique on ``bitrix_deal_id``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from AtamuraOKK.db.base import Base


class AuditVerdict(Base):
    """One close-reason audit verdict per closed-lost deal (re-audit upserts)."""

    __tablename__ = "audit_verdicts"
    __table_args__ = (
        UniqueConstraint("bitrix_deal_id", name="uq_audit_verdicts_deal"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    bitrix_deal_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    # Deal TITLE — the client label on the «Мой день» card (cheap, no extra read).
    deal_title: Mapped[str | None] = mapped_column(String(length=512))
    manager_id: Mapped[int | None] = mapped_column(
        ForeignKey("managers.id", ondelete="SET NULL"),
        index=True,
    )
    assigned_by_id: Mapped[int | None] = mapped_column(BigInteger)
    client_key: Mapped[str | None] = mapped_column(String(length=128))
    # The manager-stated close reason (resolved enum label) + its raw enum id.
    close_reason: Mapped[str | None] = mapped_column(String(length=255))
    reason_id: Mapped[str | None] = mapped_column(String(length=64))
    # supported | contradicted | not_determinable | error
    verdict: Mapped[str] = mapped_column(String(length=32), index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    justification: Mapped[str | None] = mapped_column(Text)
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    # The call ids whose transcript(s) were judged.
    call_ids: Mapped[list[Any] | None] = mapped_column(JSONB)
    # Structured evidence from a deterministic (non-LLM) check — for «Дубль…» reasons
    # the duplicate deal/contact/lead ids, the projects, and whether the manager's
    # subtype («этому» vs «другим проектам») was right. NULL for LLM-judged verdicts.
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The LLM that judged this deal — NULL when the verdict came from a deterministic
    # check instead (see `details.check`), which is the case for the «Дубль…» reasons.
    model: Mapped[str | None] = mapped_column(String(length=128))
    # When a manager nudge was sent for this verdict. Deliberately NOT written by
    # the upsert, so a re-audit never re-notifies. NULL = not yet / not applicable.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    audited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )
