"""C2 regression: scoring is idempotent.

A re-claim or duplicate broker delivery must upsert the single
(call_id, rubric_version) row, not accumulate duplicate Score rows (which silently
double-spend the LLM budget and bloat the table).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallDirection, CallStatus
from AtamuraOKK.db.models.score import Score
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.scoring.base import CallScore, CriterionScore
from AtamuraOKK.scoring.rubric import Rubric
from AtamuraOKK.scoring.rubric import load_rubric as _load_rubric
from AtamuraOKK.scoring.worker import _score_one


class _FakeScorer:
    """Returns full marks on the first call, zeros on every later call."""

    model_label = "fake/test"

    def __init__(self) -> None:
        self.calls = 0

    async def score(
        self,
        *,
        transcript: str,
        rubric: Rubric,
        direction: str,
        client_category: str | None = None,
    ) -> CallScore:
        self.calls += 1
        award = self.calls == 1
        return CallScore(
            call_type="квалификация",
            is_qualification_call=True,
            manager_identified=True,
            criteria=[
                CriterionScore(
                    id=c.id,
                    score=c.max if award else 0,
                    justification="ok",
                    evidence="",
                    recommendation="-",
                )
                for c in rubric.scored_criteria
            ],
            objections_present=True,
            sentiment_customer="нейтральный",
            sentiment_agent="нейтральный",
            summary="тест",
            red_flags=[],
            target_status="неясно",
            strengths="-",
            growth_zone="-",
            training_recommendation="-",
        )


async def test_rescoring_upserts_single_row(dbsession: AsyncSession) -> None:
    """Scoring the same call twice upserts one row reflecting the latest pass."""
    rubric = _load_rubric()
    scorer = _FakeScorer()

    call = Call(
        bitrix_call_id="score-idem-1",
        status=CallStatus.SCORING,
        direction=CallDirection.OUTBOUND,
        analyzable=True,
    )
    dbsession.add(call)
    await dbsession.flush()
    transcript = Transcript(call_id=call.id, full_text="[AGENT] привет")

    await _score_one(dbsession, call, transcript, scorer, rubric)
    await _score_one(dbsession, call, transcript, scorer, rubric)

    n = await dbsession.scalar(
        select(func.count()).select_from(Score).where(Score.call_id == call.id),
    )
    assert n == 1

    row = await dbsession.scalar(select(Score).where(Score.call_id == call.id))
    assert row is not None
    # The second (zero-marks) pass overwrote the first (full-marks) row.
    assert row.total_score is not None
    assert float(row.total_score) == 0.0
    assert scorer.calls == 2
