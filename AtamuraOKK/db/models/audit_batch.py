"""AuditBatch model — one in-flight Message Batch of close-reason judgments."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from AtamuraOKK.db.base import Base


class AuditBatch(Base):
    """A submitted batch of judge requests, tracked until its results land.

    Unlike ``score_batches`` there is no claim to heartbeat: the deals a batch covers
    are held by their own ``audit_verdicts`` row, written with ``verdict='pending'`` at
    submit time (:mod:`AtamuraOKK.audit.batch`). That row is what keeps the next audit
    pass from re-submitting the same deal, and what carries the deal context the poller
    needs to finish the verdict without re-reading Bitrix.
    """

    __tablename__ = "audit_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Provider-side batch id (``msgbatch_…``); unique so a retried submit can't
    # register the same batch twice.
    provider_batch_id: Mapped[str] = mapped_column(
        String(length=128),
        unique=True,
        nullable=False,
    )
    # Local lifecycle: in_flight -> done.
    status: Mapped[str] = mapped_column(
        String(length=32),
        nullable=False,
        default="in_flight",
    )
    # The Bitrix deal ids this batch covers, in submission order. Kept on the row
    # rather than a join table: written once, read whole.
    deal_ids: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    model: Mapped[str | None] = mapped_column(String(length=128))
    # Result tallies, filled in when the batch ends.
    succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
