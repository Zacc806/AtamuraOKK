"""Batch-API scoring: TRANSCRIBED -> SCORED at 50% of the per-call price.

Scoring the backlog one request at a time pays list price for work nobody is
waiting on. The Anthropic Message Batches API runs the same requests
asynchronously for half the cost, which is the right trade for everything except
today's calls — those stay on the realtime path in ``worker.py`` because the
cash-buyer alert only fires inside ``cash_alert_max_age_minutes``.

Two steps, each idempotent and separately runnable:

``submit`` claims a window of TRANSCRIBED calls, sends them as one batch, and
records a :class:`ScoreBatch` row. ``poll`` retrieves finished batches, persists
their scores, and releases the claims. Calls stay claimed (``SCORING``) for the
batch's whole life, so ``poll`` heartbeats ``claimed_at`` on every pass — if the
poller dies the heartbeat stops and the reconciler reclaims the calls normally.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from loguru import logger
from sqlalchemy import func, select, update

from AtamuraOKK.bitrix import get_notifier
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.models.score_batch import ScoreBatch
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.dispatch.claim import claim_ready, report_today_start
from AtamuraOKK.scoring.anthropic_scorer import build_request, parse_response
from AtamuraOKK.scoring.rubric import Rubric, load_rubric
from AtamuraOKK.scoring.worker import (
    _persist_score,
    _reconcile_transcript_labels,
    maybe_notify_cash_buyer,
)
from AtamuraOKK.settings import settings

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

_CUSTOM_ID_PREFIX = "call-"


@dataclass
class SubmitStats:
    """Summary of one submit pass."""

    claimed: int = 0
    submitted: int = 0
    batches: int = 0
    failed: int = 0


@dataclass
class PollStats:
    """Summary of one poll pass."""

    open_batches: int = 0
    ended: int = 0
    scored: int = 0
    failed: int = 0


def _client() -> AsyncAnthropic:
    from anthropic import AsyncAnthropic  # noqa: PLC0415

    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set (ATAMURAOKK_ANTHROPIC_API_KEY).",
        )
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


def _model_label() -> str:
    """Provider-prefixed model id stored on the score, matching AnthropicScorer."""
    return f"anthropic/{settings.anthropic_scoring_model}"


def _custom_id(call_id: int) -> str:
    return f"{_CUSTOM_ID_PREFIX}{call_id}"


def _call_id(custom_id: str) -> int | None:
    """Parse a call id back out of a batch result's custom_id."""
    if not custom_id.startswith(_CUSTOM_ID_PREFIX):
        return None
    raw = custom_id.removeprefix(_CUSTOM_ID_PREFIX)
    return int(raw) if raw.isdigit() else None


async def _fail(call_id: int, error: str) -> None:
    """Release a claim into FAILED so ops-retry can requeue it under max_retries.

    For genuine per-call failures only — a bad result, an unusable transcript. A
    call the model never saw is :func:`_release`d instead.
    """
    async with session_scope() as session:
        call = await session.get(Call, call_id)
        if call is None or call.status != CallStatus.SCORING:
            return
        call.attempts += 1
        call.status = CallStatus.FAILED
        call.error = error
        call.claimed_at = None


async def _release(call_id: int) -> None:
    """Put a never-scored claim straight back on the queue, no attempt burned.

    Used when the failure is transport, not scoring: a rejected submit (zero
    credits, a network blip) or a canceled/expired batch. Marking those FAILED
    would push a whole chunk — up to ``anthropic_batch_size`` calls — past
    ``max_retries`` for something no retry of *this* call could have avoided.
    """
    async with session_scope() as session:
        call = await session.get(Call, call_id)
        if call is None or call.status != CallStatus.SCORING:
            return
        call.status = CallStatus.TRANSCRIBED
        call.claimed_at = None


async def _heartbeat(call_ids: list[int]) -> None:
    """Refresh ``claimed_at`` so the reconciler doesn't reclaim a live batch.

    A batch runs far longer than ``claim_stale_seconds_score``, so without this the
    reconciler would revert the calls to TRANSCRIBED mid-flight and a later pass
    would pay to score them a second time.
    """
    if not call_ids:
        return
    async with session_scope() as session:
        await session.execute(
            update(Call)
            .where(Call.id.in_(call_ids), Call.status == CallStatus.SCORING)
            .values(claimed_at=func.now()),
        )


async def submit_pending(
    *,
    limit: int = 1000,
    since: datetime | None = None,
    include_today: bool = False,
) -> SubmitStats:
    """Claim backlog calls and submit them as Message Batches.

    Excludes today's calls by default: they belong to the realtime path, whose
    cash-buyer alert can't wait out batch latency. Pass ``include_today`` to
    override (e.g. a one-off catch-up when realtime scoring is paused).
    """
    stats = SubmitStats()
    rubric = load_rubric()
    client = _client()
    until = None if include_today else report_today_start()

    call_ids = await claim_ready(
        CallStatus.TRANSCRIBED,
        CallStatus.SCORING,
        limit,
        since=since,
        until=until,
    )
    stats.claimed = len(call_ids)
    if not call_ids:
        logger.info("Batch submit: nothing to claim.")
        return stats

    requests, covered = await _build_requests(call_ids, rubric, stats)
    for start in range(0, len(requests), settings.anthropic_batch_size):
        chunk = requests[start : start + settings.anthropic_batch_size]
        chunk_ids = covered[start : start + settings.anthropic_batch_size]
        try:
            batch = await client.messages.batches.create(requests=chunk)
        except Exception as exc:
            logger.error(
                "Batch submit failed for {n} call(s): {e}", n=len(chunk), e=exc
            )
            # Nothing was submitted, so requeue rather than fail: a rejected
            # submit is usually one shared cause (credits, network) for the whole
            # chunk, and failing them all would eat everyone's retry budget.
            for call_id in chunk_ids:
                await _release(call_id)
            stats.failed += len(chunk_ids)
            continue
        async with session_scope() as session:
            session.add(
                ScoreBatch(
                    provider_batch_id=batch.id,
                    status="in_flight",
                    call_ids=chunk_ids,
                    rubric_version=rubric.version,
                    model=_model_label(),
                ),
            )
        stats.submitted += len(chunk_ids)
        stats.batches += 1
        logger.info(
            "Submitted batch {bid}: {n} call(s)", bid=batch.id, n=len(chunk_ids)
        )
    return stats


async def _build_requests(
    call_ids: list[int],
    rubric: Rubric,
    stats: SubmitStats,
) -> tuple[list[Request], list[int]]:
    """Turn claimed ids into batch requests, failing the ones with no transcript."""
    requests: list[Request] = []
    covered: list[int] = []
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Call, Transcript)
                .join(Transcript, Transcript.call_id == Call.id, isouter=True)
                .where(Call.id.in_(call_ids)),
            )
        ).all()
    for call, transcript in rows:
        if transcript is None:
            await _fail(call.id, "no transcript")
            stats.failed += 1
            continue
        params = build_request(
            transcript=transcript.full_text,
            rubric=rubric,
            direction=str(call.direction),
            client_category=call.client_category,
            model=settings.anthropic_scoring_model,
            max_tokens=settings.anthropic_max_tokens,
        )
        requests.append(
            Request(
                custom_id=_custom_id(call.id),
                # build_request returns exactly the messages.create kwargs; the
                # cast just re-labels them as the batch API's TypedDict.
                params=cast("MessageCreateParamsNonStreaming", params),
            ),
        )
        covered.append(call.id)
    return requests, covered


async def _persist_result(call_id: int, message: Any, rubric: Rubric) -> bool:
    """Persist one succeeded batch result. Returns True when the call reached SCORED."""
    try:
        result = parse_response(message.content, message.stop_reason)
    except Exception as exc:
        logger.warning("Batch result unusable for call {id}: {e}", id=call_id, e=exc)
        await _fail(call_id, f"scoring: {exc}")
        return False

    async with session_scope() as session:
        call = await session.get(Call, call_id)
        if call is None or call.status != CallStatus.SCORING:
            return False  # reclaimed or already scored elsewhere
        try:
            await _persist_score(session, call, result, rubric, _model_label())
            transcript = await session.scalar(
                select(Transcript).where(Transcript.call_id == call_id),
            )
            if transcript is not None:
                _reconcile_transcript_labels(transcript, result)
            call.status = CallStatus.SCORED
            call.error = None
            call.claimed_at = None
            await maybe_notify_cash_buyer(session, call, result, rubric, get_notifier())
        except Exception as exc:
            call.attempts += 1
            call.status = CallStatus.FAILED
            call.error = f"scoring: {exc}"
            call.claimed_at = None
            logger.warning(
                "Scoring failed for {id}: {e}", id=call.bitrix_call_id, e=exc
            )
            return False
    return True


async def poll_batches() -> PollStats:
    """Retrieve finished batches, persist their scores, release their claims."""
    stats = PollStats()
    rubric = load_rubric()
    client = _client()

    async with session_scope() as session:
        open_batches = list(
            (
                await session.scalars(
                    select(ScoreBatch).where(ScoreBatch.status == "in_flight"),
                )
            ).all(),
        )
        pending = [
            (b.id, b.provider_batch_id, list(b.call_ids or [])) for b in open_batches
        ]
    stats.open_batches = len(pending)

    for row_id, provider_id, call_ids in pending:
        try:
            batch = await client.messages.batches.retrieve(provider_id)
        except Exception as exc:
            logger.warning("Batch {bid} retrieve failed: {e}", bid=provider_id, e=exc)
            await _heartbeat(call_ids)  # keep the claim alive; retry next pass
            continue
        if batch.processing_status != "ended":
            await _heartbeat(call_ids)
            continue

        scored, failed = await _drain_results(client, provider_id, call_ids, rubric)
        stats.ended += 1
        stats.scored += scored
        stats.failed += failed
        async with session_scope() as session:
            row = await session.get(ScoreBatch, row_id)
            if row is not None:
                row.status = "done"
                row.succeeded = scored
                row.errored = failed
                row.completed_at = func.now()
        logger.info(
            "Batch {bid} done: scored={s} failed={f}",
            bid=provider_id,
            s=scored,
            f=failed,
        )
    return stats


async def poll_until_done(*, interval_seconds: int = 60) -> PollStats:
    """Poll until no batch is left open, accumulating the pass totals.

    The claims are only heartbeated while a poll is running, so a supervised
    submit-then-drain is the intended shape: leave this running and the calls stay
    claimed; kill it and the reconciler releases them on the normal TTL.
    """
    total = PollStats()
    while True:
        stats = await poll_batches()
        total.ended += stats.ended
        total.scored += stats.scored
        total.failed += stats.failed
        total.open_batches = stats.open_batches - stats.ended
        if total.open_batches <= 0:
            logger.info(
                "Batch drain complete: scored={s} failed={f}",
                s=total.scored,
                f=total.failed,
            )
            return total
        await asyncio.sleep(interval_seconds)


async def _drain_results(
    client: AsyncAnthropic,
    provider_id: str,
    call_ids: list[int],
    rubric: Rubric,
) -> tuple[int, int]:
    """Persist every result of an ended batch; returns (scored, failed)."""
    scored = 0
    failed = 0
    seen: set[int] = set()

    async for entry in await client.messages.batches.results(provider_id):
        call_id = _call_id(entry.custom_id)
        if call_id is None:
            logger.warning(
                "Batch {bid}: unknown custom_id {c}",
                bid=provider_id,
                c=entry.custom_id,
            )
            continue
        seen.add(call_id)
        result = entry.result
        if result.type == "succeeded":
            if await _persist_result(call_id, result.message, rubric):
                scored += 1
            else:
                failed += 1
            continue
        if result.type == "errored":
            # A real per-request failure — count the attempt so a request that
            # always errors eventually dead-letters instead of looping forever.
            await _fail(call_id, f"batch errored: {result.error.error.type}")
        else:
            # canceled / expired — the model never scored it, so requeue clean.
            await _release(call_id)
        failed += 1

    # A result the batch never reported would otherwise stay claimed forever.
    for call_id in set(call_ids) - seen:
        await _release(call_id)
        failed += 1
    return scored, failed
