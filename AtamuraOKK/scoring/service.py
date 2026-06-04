"""Score a transcribed call and persist it (uses main's models + session)."""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.models.score import Score
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.scoring.alerts import notify_manipulations
from AtamuraOKK.scoring.base import CallForScoring, Scorer, ScoreResult
from AtamuraOKK.scoring.errors import ScoringError
from AtamuraOKK.scoring.history import visit_index as compute_visit_index
from AtamuraOKK.scoring.manipulation import ManipulationDetector

_MIN_TEXT_CHARS = 100


class Outcome(enum.StrEnum):
    """Result of attempting to score one call."""

    SCORED = "SCORED"
    SKIPPED = "SKIPPED"
    ALREADY_SCORED = "ALREADY_SCORED"
    FAILED = "FAILED"


@dataclass(slots=True)
class ScoringOutcome:
    """What happened when scoring a call."""

    call_id: int
    outcome: Outcome
    score_pct: float | None = None
    error: str | None = None


def _to_score(result: ScoreResult, *, call_id: int) -> Score:
    """Map a ScoreResult to a Score row (fills dashboard + subsystem columns)."""
    return Score(
        call_id=call_id,
        rubric_version=result.rubric_version,
        total_score=result.score_pct,  # dashboard headline (0-100)
        score_pct=result.score_pct,
        max_total=result.max_total,
        passed=result.passed,
        criteria=[asdict(c) for c in result.criteria],
        summary=result.summary,
        flags=list(result.red_flags),
        call_type=result.call_type,
        client_agreed_meeting=result.client_agreed_meeting,
        manager_tone=result.manager_tone,
        language=result.language,
        provider=result.provider,
        needs_human_review=result.needs_human_review,
        script_adherence=result.script_adherence,
        script_deviations=list(result.script_deviations),
        model=result.model,
        meta=result.meta,
    )


class ScoringService:
    """Score a TRANSCRIBED call and persist the score, idempotently."""

    def __init__(
        self,
        *,
        scorer: Scorer,
        rubric_version: str,
        min_duration_sec: int = 90,
        short_contact_min_sec: int = 30,
        manipulation_detector: ManipulationDetector | None = None,
    ) -> None:
        self._scorer = scorer
        self._rubric_version = rubric_version
        self._min_duration_sec = min_duration_sec
        self._short_contact_min_sec = short_contact_min_sec
        self._manipulation_detector = manipulation_detector

    async def score_call(self, session: AsyncSession, call: Call) -> ScoringOutcome:
        """Score one call (already TRANSCRIBED) and persist the result."""
        existing = await session.scalar(
            select(Score.id).where(
                Score.call_id == call.id,
                Score.rubric_version == self._rubric_version,
            ),
        )
        if existing is not None:
            return ScoringOutcome(call.id, Outcome.ALREADY_SCORED)

        if call.duration_sec < self._short_contact_min_sec:
            call.status = CallStatus.SKIPPED
            call.skip_reason = "too_short_technical"
            return ScoringOutcome(call.id, Outcome.SKIPPED)
        if call.duration_sec < self._min_duration_sec:
            call.status = CallStatus.SKIPPED
            call.skip_reason = "short_contact"
            return ScoringOutcome(call.id, Outcome.SKIPPED)

        transcript = await session.scalar(
            select(Transcript).where(Transcript.call_id == call.id),
        )
        if transcript is None or len(transcript.full_text.strip()) < _MIN_TEXT_CHARS:
            call.status = CallStatus.SKIPPED
            call.skip_reason = "empty_transcript"
            return ScoringOutcome(call.id, Outcome.SKIPPED)

        request = CallForScoring(
            text=transcript.full_text,
            duration_sec=call.duration_sec,
            language=transcript.language or "auto",
            call_ref=call.bitrix_call_id,
            visit_index=await compute_visit_index(session, call),
        )
        try:
            result = await self._scorer.score(request)
        except ScoringError as exc:
            logger.warning("scoring failed for call {id}: {e}", id=call.id, e=exc)
            call.status = CallStatus.FAILED
            call.error = str(exc)[:500]
            return ScoringOutcome(call.id, Outcome.FAILED, error=str(exc)[:500])

        score = _to_score(result, call_id=call.id)
        await self._check_manipulations(score, call, transcript.full_text)
        session.add(score)
        call.status = CallStatus.SCORED
        return ScoringOutcome(call.id, Outcome.SCORED, score_pct=result.score_pct)

    async def _check_manipulations(
        self,
        score: Score,
        call: Call,
        transcript_text: str,
    ) -> None:
        """Run the manipulation detector (if any) and attach flags/alert (ТЗ 2.1)."""
        if self._manipulation_detector is None:
            return
        manipulations = await self._manipulation_detector.detect(transcript_text)
        if not manipulations:
            return
        score.flags = [
            *(score.flags or []),
            *(f"манипуляция: {m.claim}" for m in manipulations),
        ]
        meta = dict(score.meta or {})
        meta["manipulations"] = [m.to_dict() for m in manipulations]
        score.meta = meta
        score.needs_human_review = True
        notify_manipulations(call.bitrix_call_id, manipulations)
