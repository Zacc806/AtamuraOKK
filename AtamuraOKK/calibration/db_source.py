"""Load persisted AI scores from the DB for calibration, keyed by CRM deal id.

The calibration harness compares AI :class:`ScoreResult` objects against the
human OKK xlsx. After the meeting scorer has run and written ``scores`` rows,
this rebuilds those rows into ScoreResults and joins them to a CRM deal id (via
``calls.crm_entity_id``) so :func:`AtamuraOKK.calibration.harness.compare` can
match them to the human-graded meetings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.score import Score
from AtamuraOKK.scoring.base import CriterionScore, ScoreResult

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def score_to_result(score: Score) -> ScoreResult:
    """Reconstruct a :class:`ScoreResult` from a persisted ``scores`` row.

    Only the fields the calibration harness reads (``score_pct``, ``passed``,
    ``criteria``) must be exact; the rest are restored best-effort with safe
    defaults for ``None`` columns.
    """
    criteria = [
        CriterionScore(
            id=int(c["id"]),
            block=str(c.get("block", "")),
            name=str(c.get("name", "")),
            score=int(c.get("score", 0)),
            max_score=int(c.get("max_score", 0)),
            auto=bool(c.get("auto", False)),
        )
        for c in (score.criteria or [])
    ]
    return ScoreResult(
        rubric_version=score.rubric_version or "",
        total_score=sum(c.score for c in criteria),
        max_total=score.max_total or 0,
        score_pct=float(score.score_pct or 0.0),
        passed=bool(score.passed),
        criteria=criteria,
        call_type=score.call_type or "",
        client_agreed_meeting=bool(score.client_agreed_meeting),
        manager_tone=score.manager_tone or "",
        red_flags=list(score.flags or []),
        summary=score.summary or "",
        language=score.language or "",
        provider=score.provider or "",
        model=score.model or "",
        needs_human_review=bool(score.needs_human_review),
        script_adherence=score.script_adherence,
        script_deviations=list(score.script_deviations or []),
        meta=dict(score.meta or {}),
    )


async def ai_scores_by_deal(
    session: AsyncSession,
    *,
    rubric_version: str,
) -> dict[int, ScoreResult]:
    """Map CRM deal id -> AI ScoreResult for one rubric version.

    Joins ``scores`` to ``calls`` and uses ``calls.crm_entity_id`` as the deal
    id (the same key the human xlsx is grouped by). Only deal-anchored calls are
    matched — the id is only a deal id when ``crm_entity_type == 'DEAL'`` (a
    CONTACT/LEAD/COMPANY id lives in a different id space and would mis-join).
    On duplicate deals the most recent score wins (``created_at`` ascending).
    """
    rows = (
        await session.execute(
            select(Score, Call.crm_entity_id)
            .join(Call, Score.call_id == Call.id)
            .where(
                Score.rubric_version == rubric_version,
                Call.crm_entity_type == "DEAL",
                Call.crm_entity_id.is_not(None),
            )
            .order_by(Score.created_at.asc()),
        )
    ).all()
    return {int(deal_id): score_to_result(score) for score, deal_id in rows}
