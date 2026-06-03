"""Scoring service: score a transcribed call and persist the result.

Glues the language-routed :class:`Scorer` to the DB DAOs. Used by the scoring
worker (consumes TRANSCRIBED calls) and runnable directly for a single call.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from loguru import logger

from AtamuraOKK.db.dao.call_dao import CallDAO
from AtamuraOKK.db.dao.score_dao import ScoreDAO
from AtamuraOKK.db.dao.transcript_dao import TranscriptDAO
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.scoring.base import CallForScoring, Scorer
from AtamuraOKK.scoring.errors import ScoringError

_MIN_TEXT_CHARS = 100


class Outcome(enum.StrEnum):
    """Result of attempting to score one call."""

    SCORED = "SCORED"
    SKIPPED = "SKIPPED"  # no/empty transcript — nothing to score
    ALREADY_SCORED = "ALREADY_SCORED"  # idempotent no-op
    FAILED = "FAILED"  # provider/parse failure after retries


@dataclass(slots=True)
class ScoringOutcome:
    """What happened when scoring a call."""

    call_id: int
    outcome: Outcome
    score_pct: float | None = None
    error: str | None = None


class ScoringService:
    """Score a transcribed call and persist the score, idempotently."""

    def __init__(
        self,
        *,
        scorer: Scorer,
        calls: CallDAO,
        transcripts: TranscriptDAO,
        scores: ScoreDAO,
        rubric_version: str,
    ) -> None:
        self._scorer = scorer
        self._calls = calls
        self._transcripts = transcripts
        self._scores = scores
        self._rubric_version = rubric_version

    async def score_call(self, call: Call) -> ScoringOutcome:
        """Score a single call (already TRANSCRIBED) and persist the result."""
        if await self._scores.exists(
            call_id=call.id,
            rubric_version=self._rubric_version,
        ):
            return ScoringOutcome(call.id, Outcome.ALREADY_SCORED)

        transcript = await self._transcripts.get_by_call(call.id)
        if transcript is None or len(transcript.full_text.strip()) < _MIN_TEXT_CHARS:
            await self._calls.mark(call, CallStatus.SKIPPED, error="empty transcript")
            return ScoringOutcome(call.id, Outcome.SKIPPED)

        request = CallForScoring(
            text=transcript.full_text,
            duration_sec=call.duration_sec,
            language=transcript.language,
            language_probability=transcript.language_probability or 1.0,
            call_ref=call.bitrix_call_id,
        )
        try:
            result = await self._scorer.score(request)
        except ScoringError as exc:
            logger.warning("scoring failed for call {id}: {e}", id=call.id, e=exc)
            await self._calls.mark(
                call,
                CallStatus.FAILED,
                error=str(exc)[:500],
                failed_stage="score",
            )
            return ScoringOutcome(call.id, Outcome.FAILED, error=str(exc)[:500])

        await self._scores.create_from_result(
            result,
            call_id=call.id,
            transcript_id=transcript.id,
        )
        await self._calls.mark(call, CallStatus.SCORED)
        return ScoringOutcome(call.id, Outcome.SCORED, score_pct=result.score_pct)
