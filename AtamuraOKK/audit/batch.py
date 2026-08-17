"""Batch-API judging for the close-reason audit, at 50% of the per-deal price.

Judging closed-lost deals one request at a time paid list price for work nobody is
waiting on: a verdict lands in «Отказы не по делу» for a РОП to read whenever they open
the cabinet, not on a clock. The Anthropic Message Batches API runs the same requests
(``audit/judge.py:build_request`` — byte-identical, so a verdict never depends on its
route) asynchronously for half the cost. Most batches finish inside an hour; the ceiling
is 24h.

Only the **judge** route batches. The «Дубль…» and «недозвон» routes settle against
Bitrix and Voximplant, cost no tokens, and keep landing inside the same pass — which is
most of the daily volume, so the visible queue stays nearly as fresh as before.

Two steps, each idempotent and separately runnable:

``submit_targets`` sends the requests as one batch and records an
:class:`AuditBatch`; the audit pass then writes each submitted deal's context as an
``AuditVerdict`` row with ``verdict='pending'`` — same transaction, so the batch record
and its rows land together. ``poll_batches`` retrieves finished batches and fills those
rows in with the real verdict.

The pending row is doing three jobs at once: it keeps the next pass from re-submitting
the deal (``run_audit``'s skip set covers every non-``error`` verdict), it carries the
deal context so the poller needs no second Bitrix read, and it is invisible to the
cabinet (the «Отказы не по делу» query selects ``verdict == 'contradicted'``). The audit
cursor deliberately does **not** advance past a pending deal, so if a batch is lost the
next pass re-scans and re-submits it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from loguru import logger
from sqlalchemy import select, update

from AtamuraOKK.audit.judge import build_request, parse_verdict
from AtamuraOKK.db.models.audit_batch import AuditBatch
from AtamuraOKK.db.models.audit_verdict import AuditVerdict
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.settings import settings

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from sqlalchemy.ext.asyncio import AsyncSession

PENDING_VERDICT = "pending"
_CUSTOM_ID_PREFIX = "deal-"


@dataclass
class BatchPollStats:
    """Summary of one poll pass."""

    open_batches: int = 0
    ended: int = 0
    settled: int = 0
    failed: int = 0
    verdicts: dict[str, int] | None = None


def _client() -> AsyncAnthropic:
    from anthropic import AsyncAnthropic  # noqa: PLC0415

    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ATAMURAOKK_ANTHROPIC_API_KEY is not set — cannot run the audit judge.",
        )
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


def _custom_id(deal_id: int) -> str:
    return f"{_CUSTOM_ID_PREFIX}{deal_id}"


def _deal_id(custom_id: str) -> int | None:
    """Parse a deal id back out of a batch result's custom_id."""
    if not custom_id.startswith(_CUSTOM_ID_PREFIX):
        return None
    raw = custom_id.removeprefix(_CUSTOM_ID_PREFIX)
    return int(raw) if raw.isdigit() else None


async def _drop_pending(deal_ids: list[int]) -> None:
    """Delete pending rows for deals whose batch never produced a verdict.

    Dropping the row (rather than marking it ``error``) is what puts the deal back in
    scope: the next pass re-scans it as unjudged and re-submits. An ``error`` row would
    also be retried, but it would show up in the verdict tallies as a judgment failure,
    which a lost batch is not.
    """
    if not deal_ids:
        return
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(AuditVerdict).where(
                    AuditVerdict.bitrix_deal_id.in_(deal_ids),
                    AuditVerdict.verdict == PENDING_VERDICT,
                ),
            )
        ).all()
        for row in rows:
            await session.delete(row)


async def submit_targets(
    session: AsyncSession,
    targets: list[dict[str, Any]],
) -> set[int]:
    """Submit judge targets as Message Batches; returns the deal ids that went out.

    Takes the **caller's** session rather than opening its own: the audit pass has
    uncommitted ``managers`` rows in that transaction (``ensure_managers``
    get-or-creates them), and the pending verdicts the caller writes for these deals
    carry a ``manager_id`` FK to them. A second session cannot see them and the insert
    would fail the foreign key. Writing the :class:`AuditBatch` row here too means the
    batch record, its pending rows and the cursor all commit or roll back together.

    Never raises: a rejected chunk is logged and left out of the returned set — those
    deals simply stay unjudged and the next pass re-submits them. Zero credits and a
    network blip look identical here, and neither is the deal's fault.
    """
    if not targets:
        return set()
    client = _client()
    model = settings.anthropic_scoring_model
    submitted: set[int] = set()

    for start in range(0, len(targets), settings.anthropic_batch_size):
        chunk = targets[start : start + settings.anthropic_batch_size]
        requests: list[Request] = []
        deal_ids: list[int] = []
        for x in chunk:
            params = build_request(
                transcript=x["transcript"],
                close_reason=x["reason_label"],
                notes=x.get("notes"),
                model=model,
            )
            requests.append(
                Request(
                    custom_id=_custom_id(int(x["deal"]["ID"])),
                    # build_request returns exactly the messages.create kwargs; the
                    # cast just re-labels them as the batch API's TypedDict.
                    params=cast("MessageCreateParamsNonStreaming", params),
                ),
            )
            deal_ids.append(int(x["deal"]["ID"]))

        try:
            batch = await client.messages.batches.create(requests=requests)
        except Exception as exc:
            logger.error(
                "audit batch submit failed for {n} deal(s): {e}",
                n=len(deal_ids),
                e=exc,
            )
            continue
        session.add(
            AuditBatch(
                provider_batch_id=batch.id,
                status="in_flight",
                deal_ids=deal_ids,
                model=model,
            ),
        )
        submitted.update(deal_ids)
        logger.info(
            "audit batch {bid} submitted: {n} deal(s)", bid=batch.id, n=len(deal_ids)
        )
    return submitted


async def _settle(deal_id: int, verdict: dict[str, Any]) -> str | None:
    """Write a real verdict over the deal's pending row; returns the verdict name.

    ``None`` when the row is gone or no longer pending — a re-audit on the realtime path
    may have settled it while the batch was open, and that fresher verdict wins.
    """
    name = str(verdict.get("verdict") or "error")
    async with session_scope() as session:
        row = await session.scalar(
            select(AuditVerdict).where(AuditVerdict.bitrix_deal_id == deal_id),
        )
        if row is None or row.verdict != PENDING_VERDICT:
            return None
        row.verdict = name
        row.confidence = verdict.get("confidence")
        row.justification = verdict.get("justification")
        row.evidence_quote = verdict.get("evidence_quote")
    return name


async def poll_batches() -> BatchPollStats:
    """Retrieve finished judge batches and settle the deals they cover."""
    stats = BatchPollStats(verdicts={})
    client = _client()

    async with session_scope() as session:
        open_batches = [
            (b.id, b.provider_batch_id, list(b.deal_ids or []))
            for b in (
                await session.scalars(
                    select(AuditBatch).where(AuditBatch.status == "in_flight"),
                )
            ).all()
        ]
    stats.open_batches = len(open_batches)

    for row_id, provider_id, deal_ids in open_batches:
        try:
            batch = await client.messages.batches.retrieve(provider_id)
        except Exception as exc:
            logger.warning(
                "audit batch {bid} retrieve failed: {e}", bid=provider_id, e=exc
            )
            continue
        if batch.processing_status != "ended":
            continue

        settled, failed = await _drain(client, provider_id, deal_ids, stats)
        stats.ended += 1
        stats.settled += settled
        stats.failed += failed
        async with session_scope() as session:
            await session.execute(
                update(AuditBatch)
                .where(AuditBatch.id == row_id)
                .values(
                    status="done",
                    succeeded=settled,
                    errored=failed,
                    completed_at=batch.ended_at,
                ),
            )
        logger.info(
            "audit batch {bid} done: settled={s} failed={f} verdicts={v}",
            bid=provider_id,
            s=settled,
            f=failed,
            v=stats.verdicts,
        )
    return stats


async def _drain(
    client: AsyncAnthropic,
    provider_id: str,
    deal_ids: list[int],
    stats: BatchPollStats,
) -> tuple[int, int]:
    """Settle every result of an ended batch; returns (settled, failed)."""
    settled = 0
    failed = 0
    seen: set[int] = set()
    tallies = stats.verdicts if stats.verdicts is not None else {}

    async for entry in await client.messages.batches.results(provider_id):
        deal_id = _deal_id(entry.custom_id)
        if deal_id is None:
            logger.warning(
                "audit batch {bid}: unknown custom_id {c}",
                bid=provider_id,
                c=entry.custom_id,
            )
            continue
        seen.add(deal_id)
        result = entry.result
        if result.type != "succeeded":
            # errored / canceled / expired — the judge never saw it. Drop the pending
            # row so the next pass re-submits rather than leaving a deal parked.
            await _drop_pending([deal_id])
            failed += 1
            continue
        message = result.message
        if message.stop_reason == "max_tokens":
            # A truncated forced tool call is incomplete JSON, not a verdict.
            await _drop_pending([deal_id])
            failed += 1
            continue
        name = await _settle(deal_id, parse_verdict(message.content))
        if name is None:
            failed += 1
            continue
        tallies[name] = tallies.get(name, 0) + 1
        settled += 1

    # A deal the batch never reported would otherwise stay pending forever.
    missing = [d for d in deal_ids if d not in seen]
    if missing:
        logger.warning(
            "audit batch {bid}: {n} deal(s) had no result — requeued",
            bid=provider_id,
            n=len(missing),
        )
        await _drop_pending(missing)
        failed += len(missing)
    return settled, failed
