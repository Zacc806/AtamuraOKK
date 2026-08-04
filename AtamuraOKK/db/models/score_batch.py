"""ScoreBatch model — one in-flight Anthropic Message Batch of scoring requests."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from AtamuraOKK.db.base import Base


class ScoreBatch(Base):
    """A submitted batch of scoring requests, tracked until its results land.

    The calls it covers are held claimed (``calls.status = SCORING``) for the
    batch's whole lifetime, which is far longer than ``claim_stale_seconds_score``
    — :func:`AtamuraOKK.scoring.batch.poll_batches` therefore heartbeats their
    ``claimed_at`` on every pass. If the poller stops, the heartbeat stops with it
    and the reconciler correctly reclaims the calls.
    """

    __tablename__ = "score_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Provider-side batch id (``msgbatch_…``); unique so a retried submit can't
    # register the same batch twice.
    provider_batch_id: Mapped[str] = mapped_column(
        String(length=128),
        unique=True,
        nullable=False,
    )
    # Local lifecycle: in_flight -> done (or failed when the batch itself errored).
    status: Mapped[str] = mapped_column(
        String(length=32),
        nullable=False,
        default="in_flight",
    )
    # The claimed call ids this batch covers, in submission order. Kept on the row
    # rather than a join table: it is written once and read whole.
    call_ids: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    rubric_version: Mapped[str | None] = mapped_column(String(length=64))
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
