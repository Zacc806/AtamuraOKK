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
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.models.score import Score
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.scoring.base import CallScore, Scorer
from AtamuraOKK.scoring.openai_scorer import OpenAIScorer
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
    total = 0

    for crit in rubric.scored_criteria:
        cs = by_id.get(crit.id)
        score = cs.score if cs else 0
        # Enforce: full marks for objection criteria when no objection occurred.
        if crit.block_id == "objections" and not result.objections_present:
            score = crit.max
        score = max(0, min(int(score), crit.max))
        total += score
        per_criterion.append(
            {
                "id": crit.id,
                "block_id": crit.block_id,
                "block_name": crit.block_name,
                "text": crit.text,
                "score": score,
                "max": crit.max,
                "justification": cs.justification if cs else "не оценено",
                "evidence": cs.evidence if cs else "",
            },
        )
        b = blocks.setdefault(
            crit.block_id,
            {"name": crit.block_name, "score": 0, "max": 0},
        )
        b["score"] += score
        b["max"] += crit.max

    max_points = rubric.max_conversational
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
    model: str,
) -> None:
    result = await scorer.score(
        transcript=transcript.full_text,
        rubric=rubric,
        direction=str(call.direction),
    )
    payload = _assemble(result, rubric)
    session.add(
        Score(
            call_id=call.id,
            rubric_version=rubric.version,
            total_score=payload["percent"],
            criteria=payload,
            sentiment={
                "customer": result.sentiment_customer,
                "agent": result.sentiment_agent,
            },
            summary=result.summary,
            flags=result.red_flags,
            model=f"openai/{model}",
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


async def score_pending(*, limit: int = 50) -> ScoreStats:
    """Score analyzable TRANSCRIBED calls against the active rubric."""
    stats = ScoreStats()
    rubric = load_rubric()
    scorer = OpenAIScorer()

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Call, Transcript)
                .join(Transcript, Transcript.call_id == Call.id)
                .where(
                    Call.status == CallStatus.TRANSCRIBED,
                    Call.analyzable.is_(True),
                )
                .order_by(Call.started_at.asc())
                .limit(limit),
            )
        ).all()

        for call, transcript in rows:
            stats.attempted += 1
            try:
                await _score_one(
                    session,
                    call,
                    transcript,
                    scorer,
                    rubric,
                    scorer.model,
                )
                stats.scored += 1
            except Exception as exc:  # record + continue to next call
                call.attempts += 1
                call.status = CallStatus.FAILED
                call.error = f"scoring: {exc}"
                stats.failed += 1
                logger.warning(
                    "Scoring failed for {id}: {e}",
                    id=call.bitrix_call_id,
                    e=exc,
                )

    logger.info(
        "Scoring done: attempted={a} scored={s} failed={f}",
        a=stats.attempted,
        s=stats.scored,
        f=stats.failed,
    )
    return stats
