"""Scoring worker: TRANSCRIBED -> SCORED.

Scores each analyzable transcribed call against the active rubric, derives the
numeric total / percent / zone, and persists a Score row. The conversational
percent (over the 91 audio-derivable points) is the headline metric.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.models.score import Score
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.dispatch.claim import claim_ready
from AtamuraOKK.scoring.base import CallScore, Scorer
from AtamuraOKK.scoring.factory import get_scorer
from AtamuraOKK.scoring.rubric import Rubric, load_rubric


@dataclass
class ScoreStats:
    """Summary of one scoring pass."""

    attempted: int = 0
    scored: int = 0
    failed: int = 0


def _assemble(result: CallScore, rubric: Rubric) -> dict[str, Any]:
    """Apply rubric maxima + objection rule; build the persisted score payload."""
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
        cs = by_id.get(crit.id)
        if cs is None:
            # A criterion the model didn't return would be silently scored 0,
            # deflating the result; fail the call instead so it retries.
            missing.append(crit.id)
            continue
        score = cs.score
        score = max(0, min(int(score), crit.max))
        total += score
        max_points += crit.max
        per_criterion.append(
            {
                "id": crit.id,
                "block_id": crit.block_id,
                "block_name": crit.block_name,
                "text": crit.text,
                "score": score,
                "max": crit.max,
                "justification": cs.justification,
                "evidence": cs.evidence,
            },
        )
        b = blocks.setdefault(
            crit.block_id,
            {"name": crit.block_name, "score": 0, "max": 0},
        )
        b["score"] += score
        b["max"] += crit.max

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
        "target_status": result.target_status,
        "strengths": result.strengths,
        "growth_zone": result.growth_zone,
        "training_recommendation": result.training_recommendation,
    }


async def _score_one(
    session: AsyncSession,
    call: Call,
    transcript: Transcript,
    scorer: Scorer,
    rubric: Rubric,
) -> None:
    result = await scorer.score(
        transcript=transcript.full_text,
        rubric=rubric,
        direction=str(call.direction),
    )
    payload = _assemble(result, rubric)
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
        "model": scorer.model_label,
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
    call.status = CallStatus.SCORED
    call.error = None
    logger.info(
        "Scored {id}: {pct}% ({zone})",
        id=call.bitrix_call_id,
        pct=payload["percent"],
        zone=payload["zone"],
    )


async def _score_claimed(
    session: AsyncSession,
    call: Call,
    transcript: Transcript,
    scorer: Scorer,
    rubric: Rubric,
) -> str:
    """Score one already-claimed (SCORING) call. Caller commits.

    Mutates ``call`` to SCORED / FAILED, clears the claim; returns the status.
    """
    try:
        await _score_one(session, call, transcript, scorer, rubric)
    except Exception as exc:  # record + move on
        call.attempts += 1
        call.status = CallStatus.FAILED
        call.error = f"scoring: {exc}"
        logger.warning("Scoring failed for {id}: {e}", id=call.bitrix_call_id, e=exc)
    call.claimed_at = None
    return call.status.value


async def score_one(
    call_id: int,
    *,
    scorer: Scorer | None = None,
    rubric: Rubric | None = None,
) -> str:
    """Score one claimed (SCORING) call in its own session.

    The unit of work for the broker task. Returns the resulting status value, or
    ``"skipped"`` if no longer claimed for scoring.
    """
    scorer = scorer or get_scorer()
    rubric = rubric or load_rubric()
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
        return await _score_claimed(session, call, transcript, scorer, rubric)


async def score_pending(*, limit: int = 50) -> ScoreStats:
    """Claim and score analyzable TRANSCRIBED calls against the active rubric."""
    stats = ScoreStats()
    rubric = load_rubric()
    scorer = get_scorer()

    call_ids = await claim_ready(CallStatus.TRANSCRIBED, CallStatus.SCORING, limit)
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
