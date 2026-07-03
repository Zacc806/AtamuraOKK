"""Scoring worker: TRANSCRIBED -> SCORED.

Scores each analyzable transcribed call against the active rubric, derives the
numeric percent / zone, and persists a Score row. Under ``tm-call-v4`` the
headline percent is the equal-weight average of the applicable blocks' binary
pass rates (see :func:`_assemble`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.bitrix import BitrixNotifier, crm_card_url, get_notifier
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.db.models.score import Score
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.dispatch.claim import auto_since, claim_ready
from AtamuraOKK.scoring.base import CallScore, Scorer
from AtamuraOKK.scoring.factory import get_scorer
from AtamuraOKK.scoring.rubric import Rubric, load_rubric
from AtamuraOKK.settings import settings


@dataclass
class ScoreStats:
    """Summary of one scoring pass."""

    attempted: int = 0
    scored: int = 0
    failed: int = 0


def _assemble(
    result: CallScore,
    rubric: Rubric,
    category: str | None = None,
) -> dict[str, Any]:
    """Build the score payload from binary element verdicts (flat model).

    Each element is ДА=1 / НЕТ=0 / Н.П. (excluded). The call percent is
    ``ДА ÷ applicable × 100`` across **all** applicable elements — every element
    weighs the same; blocks only group elements (for the breakdown and the
    Н.П. rules) and do not weight the total. A Н.П. element is dropped from the
    denominator (not scored 0); when a whole block is Н.П. — the objections block
    with no objection, or a block of only conditional items that all fell away —
    its elements simply contribute nothing to numerator or denominator.

    ``category`` (the client's A/B/C/X lead category) is accepted for interface
    compatibility but not used: the v4 sheet replaced category weighting with
    per-element Н.П. It is still recorded on the payload for reporting.
    """
    by_id = {c.id: c for c in result.criteria}
    per_criterion: list[dict[str, Any]] = []
    blocks: dict[str, dict[str, Any]] = {}
    missing: list[int] = []

    for block in rubric.block_list:
        # No objection occurred -> the whole objections block is Н.П.; it drops out
        # of the average so the percent reflects only what the call exercised.
        if block.na_if_no_objections and not result.objections_present:
            continue
        block_yes = 0
        applicable = 0
        block_entries: list[dict[str, Any]] = []
        for crit in block.criteria:
            cs = by_id.get(crit.id)
            if cs is None:
                # A mandatory element the model didn't return would be silently
                # scored 0, deflating the result -> fail the call so it retries.
                # A conditionally-Н.П. element (na_allowed) may be legitimately
                # omitted; treat the omission as Н.П. rather than a failure.
                if not crit.na_allowed:
                    missing.append(crit.id)
                continue
            # Н.П. only where the sheet allows it; anywhere else the element is
            # always scored so it can't be dropped to inflate the block percent.
            if crit.na_allowed and not cs.applicable:
                continue
            score = 1 if int(cs.score) >= 1 else 0
            block_yes += score
            applicable += 1
            block_entries.append(
                {
                    "id": crit.id,
                    "block_id": crit.block_id,
                    "block_name": crit.block_name,
                    "text": crit.text,
                    "score": score,
                    "max": 1,
                    "justification": cs.justification,
                    "evidence": cs.evidence,
                    "recommendation": cs.recommendation,
                },
            )
        if applicable == 0:
            # Every element in the block was Н.П. -> block drops from the average.
            continue
        per_criterion.extend(block_entries)
        blocks[block.id] = {
            "name": block.name,
            "score": block_yes,
            "max": applicable,
            "percent": round(100.0 * block_yes / applicable, 2),
        }

    if missing:
        raise ValueError(f"scorer omitted criteria: {sorted(missing)}")

    # Flat model: the call percent is ДА ÷ applicable across ALL applicable
    # elements (each element weighs the same; blocks do not weight the total).
    # blocks[*]["percent"] is kept as a per-block breakdown for display only.
    raw_points = sum(b["score"] for b in blocks.values())
    max_points = sum(b["max"] for b in blocks.values())
    percent = round(100.0 * raw_points / max_points, 2) if max_points else 0.0
    return {
        "per_criterion": per_criterion,
        "blocks": blocks,
        "raw_points": raw_points,
        "max_points": max_points,
        "percent": percent,
        "zone": rubric.zone_for(percent),
        "call_type": result.call_type,
        "is_qualification_call": result.is_qualification_call,
        "manager_identified": result.manager_identified,
        "manager_side": result.manager_side,
        "objections_present": result.objections_present,
        "client_category": category,
        "target_status": result.target_status,
        "strengths": result.strengths,
        "growth_zone": result.growth_zone,
        "training_recommendation": result.training_recommendation,
        "payment_method": result.payment_method,
        "wants_to_visit": result.wants_to_visit,
        "on_premises": result.on_premises,
    }


def _swap_speaker_labels(
    segments: list[dict[str, Any]] | None,
    full_text: str | None,
) -> tuple[list[dict[str, Any]] | None, str]:
    """Swap the agent/customer labels in segments and full_text, body preserved.

    The stereo channel->role guess is inverted on some calls, so the manager is
    stored as the customer (and vice versa). This swaps only the role labels — the
    ``speaker`` field of each segment and the ``[AGENT]``/``[CUSTOMER]`` block
    headers in ``full_text`` — without touching the spoken text. Rebuilding
    ``full_text`` from segments is deliberately avoided: entity correction edits
    ``full_text`` only (not the raw segments), so a rebuild would drop those fixes.
    """
    swap = {"agent": "customer", "customer": "agent"}
    new_segments: list[dict[str, Any]] | None = segments
    if segments:
        new_segments = [
            {**s, "speaker": swap.get(spk := str(s.get("speaker", "")), spk)}
            for s in segments
        ]
    sentinel = "\x00"
    new_full = (
        (full_text or "")
        .replace("[AGENT]", sentinel)
        .replace("[CUSTOMER]", "[AGENT]")
        .replace(sentinel, "[CUSTOMER]")
    )
    return new_segments, new_full


def _reconcile_transcript_labels(transcript: Transcript, result: CallScore) -> None:
    """Make speaker=="agent" the content-identified manager, idempotently.

    The scorer reports ``manager_side`` ("A" = the side currently labeled agent,
    "B" = labeled customer). When the manager is on side "B" the stereo channel
    mapping was inverted for this call, so swap the labels. Marks the transcript
    ``manager_side_applied`` once a definite side ("A"/"B") is determined so a
    re-score never double-flips; "unknown" leaves it unreconciled for a later pass.
    """
    if result.manager_side == "unknown" or transcript.manager_side_applied:
        return
    if result.manager_side == "B":
        transcript.segments, transcript.full_text = _swap_speaker_labels(
            transcript.segments, transcript.full_text
        )
    transcript.manager_side_applied = True


async def _persist_score(
    session: AsyncSession,
    call: Call,
    result: CallScore,
    rubric: Rubric,
    model_label: str,
) -> None:
    """Assemble + upsert the Score row for a call. Caller sets status / commits."""
    payload = _assemble(result, rubric, call.client_category)
    values = {
        "call_id": call.id,
        "rubric_version": rubric.version,
        "total_score": payload["percent"],
        "criteria": payload,
        "sentiment": {
            "customer": result.sentiment_customer,
            "agent": result.sentiment_agent,
        },
        "summary": result.summary,
        "flags": result.red_flags,
        "model": model_label,
    }
    # Upsert: a re-claim or duplicate delivery must not create a second row.
    stmt = insert(Score).values(**values)
    update_cols = {c: stmt.excluded[c] for c in values if c not in ("call_id",)}
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_scores_call_rubric",
            set_=update_cols,
        ),
    )
    logger.info(
        "Scored {id}: {pct}% ({zone})",
        id=call.bitrix_call_id,
        pct=payload["percent"],
        zone=payload["zone"],
    )


def should_notify_cash(
    result: CallScore,
    *,
    started_at: datetime | None,
    now: datetime,
    max_age_minutes: int,
) -> bool:
    """Whether a scored call should trigger a cash-buyer manager alert.

    Fires only for a genuine qualification call where the client pays cash, and
    only while the call is recent — the age guard keeps a backfill / ``run --all``
    rescore from notifying managers about long-past calls.
    """
    if result.payment_method != "наличные" or not result.is_qualification_call:
        return False
    if started_at is None:
        return False
    # ``now`` is tz-aware (UTC); coerce a naive ``started_at`` so the subtraction
    # can't raise and silently suppress the alert on this Bitrix-write path.
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return now - started_at <= timedelta(minutes=max_age_minutes)


def _cash_alert_message(call: Call, result: CallScore) -> str:
    """Manager-facing notification body for a cash-buyer call."""
    lines = ["💰 Клиент готов оплатить наличными."]
    if result.on_premises:
        lines.append("📍 Клиент уже в офисе / на объекте.")
    elif result.wants_to_visit:
        lines.append("🚗 Клиент готов приехать на встречу / просмотр.")
    if result.summary:
        lines.append(f"Резюме звонка: {result.summary}")
    url = crm_card_url(call.crm_entity_type, call.crm_entity_id)
    if url:
        lines.append(url)
    return "\n".join(lines)


async def maybe_notify_cash_buyer(
    session: AsyncSession,
    call: Call,
    result: CallScore,
    rubric: Rubric,
    notifier: BitrixNotifier,
) -> None:
    """Notify the responsible manager when a scored call is a cash buyer.

    Idempotent via ``scores.notified_at`` (survives re-score upserts), and fully
    guarded: a Bitrix failure is logged and swallowed so it can never fail the
    just-scored call (the caller keeps the call at ``SCORED``).
    """
    if not should_notify_cash(
        result,
        started_at=call.started_at,
        now=datetime.now(UTC),
        max_age_minutes=settings.cash_alert_max_age_minutes,
    ):
        return
    try:
        score = await session.scalar(
            select(Score).where(
                Score.call_id == call.id,
                Score.rubric_version == rubric.version,
            ),
        )
        if score is None or score.notified_at is not None:
            return
        if call.manager_id is None:
            logger.warning(
                "cash alert: call {id} has no manager — skipping",
                id=call.bitrix_call_id,
            )
            return
        bitrix_user_id = await session.scalar(
            select(Manager.bitrix_user_id).where(Manager.id == call.manager_id),
        )
        if bitrix_user_id is None:
            logger.warning(
                "cash alert: manager {mid} has no Bitrix id — skipping",
                mid=call.manager_id,
            )
            return
        await notifier.send(int(bitrix_user_id), _cash_alert_message(call, result))
        score.notified_at = func.now()
        logger.info(
            "cash alert sent to user {uid} for call {id}",
            uid=bitrix_user_id,
            id=call.bitrix_call_id,
        )
    except Exception as exc:  # never fail the score on a notification error
        logger.warning(
            "cash alert failed for call {id}: {e}", id=call.bitrix_call_id, e=exc
        )


async def _score_one(
    session: AsyncSession,
    call: Call,
    transcript: Transcript,
    scorer: Scorer,
    rubric: Rubric,
    notifier: BitrixNotifier | None = None,
) -> None:
    result = await scorer.score(
        transcript=transcript.full_text,
        rubric=rubric,
        direction=str(call.direction),
        client_category=call.client_category,
    )
    await _persist_score(session, call, result, rubric, scorer.model_label)
    _reconcile_transcript_labels(transcript, result)
    call.status = CallStatus.SCORED
    call.error = None
    await maybe_notify_cash_buyer(
        session, call, result, rubric, notifier or get_notifier()
    )


async def score_one(
    call_id: int,
    *,
    scorer: Scorer | None = None,
    rubric: Rubric | None = None,
    notifier: BitrixNotifier | None = None,
) -> str:
    """Score one claimed (SCORING) call without holding a DB connection.

    Three short transactions bracket the slow LLM call: verify the claim and read
    the transcript, release the connection for the LLM round-trip, then reacquire
    to persist the score (re-verifying the claim so a duplicate delivery returns
    ``"skipped"``). Returns the resulting status value.
    """
    scorer = scorer or get_scorer()
    rubric = rubric or load_rubric()
    notifier = notifier or get_notifier()

    async with session_scope() as session:
        call = await session.get(Call, call_id)
        if call is None or call.status != CallStatus.SCORING:
            return "skipped"
        transcript = await session.scalar(
            select(Transcript).where(Transcript.call_id == call_id),
        )
        if transcript is None:
            call.status = CallStatus.FAILED
            call.error = "no transcript"
            call.claimed_at = None
            return call.status.value
        transcript_text = transcript.full_text
        direction = str(call.direction)
        category = call.client_category

    result: CallScore | None = None
    error: str | None = None
    try:
        result = await scorer.score(
            transcript=transcript_text,
            rubric=rubric,
            direction=direction,
            client_category=category,
        )
    except Exception as exc:  # record + move on
        error = f"scoring: {exc}"
        logger.warning("Scoring failed for call {id}: {e}", id=call_id, e=exc)

    async with session_scope() as session:
        call = await session.get(Call, call_id)
        if call is None or call.status != CallStatus.SCORING:
            return "skipped"
        if result is not None:
            try:
                await _persist_score(session, call, result, rubric, scorer.model_label)
                transcript = await session.scalar(
                    select(Transcript).where(Transcript.call_id == call_id),
                )
                if transcript is not None:
                    _reconcile_transcript_labels(transcript, result)
                call.status = CallStatus.SCORED
                call.error = None
                await maybe_notify_cash_buyer(session, call, result, rubric, notifier)
            except Exception as exc:  # record + move on
                call.attempts += 1
                call.status = CallStatus.FAILED
                call.error = f"scoring: {exc}"
                logger.warning(
                    "Scoring failed for {id}: {e}", id=call.bitrix_call_id, e=exc
                )
        else:
            call.attempts += 1
            call.status = CallStatus.FAILED
            call.error = error
        call.claimed_at = None
        return call.status.value


class _Auto:
    """Sentinel for ``score_pending(since=...)``'s 'use the auto window' default."""


_AUTO = _Auto()


async def score_pending(
    *, limit: int = 50, since: datetime | None | _Auto = _AUTO
) -> ScoreStats:
    """Claim and score analyzable TRANSCRIBED calls against the active rubric.

    ``since`` defaults to :func:`auto_since` (today-only when the knob is set), so
    the safe automatic window is the default and no caller can accidentally score
    the whole paid backlog by forgetting the cutoff. Pass an explicit ``since=None``
    to score the full backlog (the manual ``run --all`` path), or a datetime to
    score a custom window.
    """
    window = auto_since() if isinstance(since, _Auto) else since
    stats = ScoreStats()
    rubric = load_rubric()
    scorer = get_scorer()

    call_ids = await claim_ready(
        CallStatus.TRANSCRIBED, CallStatus.SCORING, limit, since=window
    )
    for call_id in call_ids:
        status = await score_one(call_id, scorer=scorer, rubric=rubric)
        if status == "skipped":
            continue
        stats.attempted += 1
        if status == CallStatus.SCORED.value:
            stats.scored += 1
        elif status == CallStatus.FAILED.value:
            stats.failed += 1

    logger.info(
        "Scoring done: attempted={a} scored={s} failed={f}",
        a=stats.attempted,
        s=stats.scored,
        f=stats.failed,
    )
    return stats


async def requeue_scored_for_relabel(
    *, limit: int | None = None, stereo_only: bool = True
) -> int:
    """Revert SCORED calls to TRANSCRIBED so the next scoring pass relabels them.

    One-time backfill for the inverted-roles fix: re-scoring reconciles each
    transcript's speaker labels with the content-identified manager (only
    transcripts with ``manager_side_applied=false`` are touched, so this is safe to
    run, but re-scoring costs one LLM call per call — keep it scoped). Defaults to
    stereo calls only, the only ones the channel->role mapping can invert. Returns
    the count requeued.
    """
    from sqlalchemy import update  # noqa: PLC0415

    async with session_scope() as session:
        stmt = select(Call.id).where(Call.status == CallStatus.SCORED)
        if stereo_only:
            stmt = stmt.where(Call.is_stereo.is_(True))
        if limit is not None:
            stmt = stmt.limit(limit)
        ids = list((await session.execute(stmt)).scalars().all())
        if not ids:
            logger.info("No SCORED calls to requeue for relabel.")
            return 0
        await session.execute(
            update(Call)
            .where(Call.id.in_(ids))
            .values(status=CallStatus.TRANSCRIBED, error=None, claimed_at=None),
        )
    logger.info("Requeued {n} SCORED call(s) -> TRANSCRIBED for relabel", n=len(ids))
    return len(ids)
