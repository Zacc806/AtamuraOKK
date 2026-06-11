"""Seed the active rubrics into the ``rubric_versions`` table (provenance).

One active rubric per ``source`` (department axis): the ТМ call rubric under
"tm" and the ОП meeting rubric under "op". Score-time rubric loading stays
file-based in both pipelines — these rows are provenance plus the read
surface for the companion ``GET /api/v1/rubrics``.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.models.rubric_version import RubricVersion
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.scoring.meetings.config import config as meetings_config
from AtamuraOKK.scoring.meetings.rubric import load_rubric as load_meeting_rubric
from AtamuraOKK.scoring.rubric import DEFAULT_VERSION, load_rubric

CALL_SOURCE = "tm"


async def _seed_one(
    session: AsyncSession,
    *,
    source: str,
    version: str,
    definition: dict[str, Any],
) -> None:
    """Upsert one rubric as its source's single active row."""
    await session.execute(
        update(RubricVersion)
        .where(RubricVersion.source == source)
        .values(active=False),
    )
    stmt = insert(RubricVersion).values(
        source=source,
        version=version,
        definition=definition,
        active=True,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_rubric_versions_source_version",
        set_={"definition": definition, "active": True},
    )
    await session.execute(stmt)
    logger.info("Seeded active rubric {v} (source {s})", v=version, s=source)


async def _seed_all(session: AsyncSession) -> None:
    call_rubric = load_rubric(DEFAULT_VERSION)
    await _seed_one(
        session,
        source=CALL_SOURCE,
        version=call_rubric.version,
        definition=call_rubric.raw,
    )
    meeting_rubric = load_meeting_rubric(meetings_config.score_meeting_rubric_version)
    # Keyed by rubric.id (file stem, e.g. "okk_meeting_v1") — that is what
    # ``meetings.rubric_version`` stores, not the JSON's inner version string.
    await _seed_one(
        session,
        source=meetings_config.meetings_source,
        version=meeting_rubric.id,
        definition=meeting_rubric.to_definition(),
    )


async def seed_active_rubrics(session: AsyncSession | None = None) -> None:
    """Upsert the call (tm) and meeting (op) rubrics, one active per source.

    With ``session`` given (tests) the caller owns the transaction; otherwise
    a ``session_scope()`` commits.
    """
    if session is not None:
        await _seed_all(session)
        return
    async with session_scope() as scoped:
        await _seed_all(scoped)
