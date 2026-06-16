"""Scoring worker: TRANSCRIBED -> SCORED.

Scores each analyzable transcribed call against the active rubric, derives the
numeric total / percent / zone, and persists a Score row. The conversational
percent (over the 91 audio-derivable points) is the headline metric.
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
from AtamuraOKK.dispatch.claim import claim_ready
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
    """Apply rubric maxima + objection/category rules; build the score payload.

    ``category`` is the client's lead category (A/B/C/X) — it re-weights the
    meeting-closing criterion: A/None/X keep the full max, B uses a reduced max,
    C drops it from numerator and denominator entirely (clients not expected to
    agree to a meeting). See ``Rubric.max_for``.
    """
    by_id = {c.id: c for c in result.criteria}
    per_criterion: list[dict[str, Any]] = []
    blocks: dict[str, dict[str, Any]] = {}
    missing: list[int] = []
    total = 0
    max_points = 0

    for crit in rubric.scored_criteria:
        # No objection occurred -> the objection block wasn't testable; exclude it
        # from the score entirely (not in numerator nor denominator) so the percent
        # reflects only what the call actually exercised.
        if crit.block_id == "objections" and not result.objections_present:
            continue
        # Per-category weight: a 0-weight category (C on the closing block) excludes
        # the criterion entirely, exactly like the no-objections rule; B carries a
        # reduced max. Checked before the missing-criterion guard so an excluded
        # criterion the model legitimately omits doesn't fail the call.
        eff_max = rubric.max_for(crit, category)
        if eff_max is None:
            continue
        cs = by_id.get(crit.id)
        if cs is None:
            # A criterion the model didn't return would be silently scored 0,
            # deflating the result; fail the call instead so it retries.
            missing.append(crit.id)
            continue
        score = max(0, min(int(cs.score), eff_max))
        total += score
        max_points += eff_max
        per_criterion.append(
            {
                "id": crit.id,
                "block_id": crit.block_id,
                "block_name": crit.block_name,
                "text": crit.text,
                "score": score,
                "max": eff_max,
                "justification": cs.justification,
                "evidence": cs.evidence,
                "recommendation": cs.recommendation,
            },
        )
        b = blocks.setdefault(
            crit.block_id,
            {"name": crit.block_name, "score": 0, "max": 0},
        )
        b["score"] += score
        b["max"] += eff_max

    if missing:
        raise ValueError(f"scorer omitted criteria: {missing}")

    percent = round(100.0 * total / max_points, 2) if max_points else 0.0
    return {
        "per_criterion": per_criterion,
        "blocks": blocks,
        "raw_points": total,
        "max_points": max_points,
        "percent": percent,
        "zone": rubric.zone_for(percent),
        "call_type": result.call_type,
        "is_qualification_call": result.is_qualification_call,
        "manager_identified": result.manager_identified,
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


async def score_pending(
    *, limit: int = 50, since: datetime | None = None
) -> ScoreStats:
    """Claim and score analyzable TRANSCRIBED calls against the active rubric.

    When ``since`` is given, only calls that started at or after it are claimed;
    pass ``None`` to score the full backlog (the manual ``run --all`` path).
    """
    stats = ScoreStats()
    rubric = load_rubric()
    scorer = get_scorer()

    call_ids = await claim_ready(
        CallStatus.TRANSCRIBED, CallStatus.SCORING, limit, since=since
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
