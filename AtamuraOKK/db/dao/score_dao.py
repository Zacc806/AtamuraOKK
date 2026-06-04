"""Data access for scores and rubric versions."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.dependencies import get_db_session
from AtamuraOKK.db.models.rubric_version import RubricVersion
from AtamuraOKK.db.models.score import Score
from AtamuraOKK.scoring.base import ScoreResult


class ScoreDAO:
    """Read/write access to the ``scores`` table."""

    def __init__(self, session: AsyncSession = Depends(get_db_session)) -> None:
        self.session = session

    async def create_from_result(
        self,
        result: ScoreResult,
        *,
        call_id: int,
        transcript_id: int | None = None,
    ) -> Score:
        """Persist a :class:`ScoreResult` as a ``scores`` row."""
        score = Score(
            call_id=call_id,
            transcript_id=transcript_id,
            rubric_version=result.rubric_version,
            total_score=result.total_score,
            max_total=result.max_total,
            score_pct=result.score_pct,
            passed=result.passed,
            criteria=[asdict(c) for c in result.criteria],
            client_agreed_meeting=result.client_agreed_meeting,
            manager_tone=result.manager_tone,
            red_flags=list(result.red_flags),
            summary=result.summary,
            language=result.language,
            provider=result.provider,
            model=result.model,
            needs_human_review=result.needs_human_review,
            script_adherence=result.script_adherence,
            script_deviations=list(result.script_deviations),
            meta=result.meta,
        )
        self.session.add(score)
        await self.session.flush()
        return score

    async def latest_for_call(self, call_id: int) -> Score | None:
        """Most recent score for a call, or None."""
        stmt = (
            select(Score)
            .where(Score.call_id == call_id)
            .order_by(Score.id.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def exists(self, *, call_id: int, rubric_version: str) -> bool:
        """Whether a call already has a score under the given rubric version."""
        stmt = select(Score.id).where(
            Score.call_id == call_id,
            Score.rubric_version == rubric_version,
        )
        result = await self.session.execute(stmt)
        return result.first() is not None


class RubricVersionDAO:
    """Read/write access to the ``rubric_versions`` table."""

    def __init__(self, session: AsyncSession = Depends(get_db_session)) -> None:
        self.session = session

    async def upsert(self, version: str, definition: dict[str, Any]) -> RubricVersion:
        """Insert the rubric snapshot if absent; return the row."""
        stmt = select(RubricVersion).where(RubricVersion.version == version)
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return existing
        row = RubricVersion(version=version, definition=definition, active=True)
        self.session.add(row)
        await self.session.flush()
        return row
