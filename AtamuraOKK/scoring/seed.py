"""Seed the active rubric into the ``rubric_versions`` table (provenance)."""

from __future__ import annotations

from loguru import logger
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert

from AtamuraOKK.db.models.rubric_version import RubricVersion
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.scoring.rubric import DEFAULT_VERSION, load_rubric


async def seed_active_rubric(version: str = DEFAULT_VERSION) -> None:
    """Upsert the rubric JSON as the single active ``rubric_versions`` row."""
    rubric = load_rubric(version)
    async with session_scope() as session:
        await session.execute(update(RubricVersion).values(active=False))
        stmt = insert(RubricVersion).values(
            version=rubric.version,
            definition=rubric.raw,
            active=True,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["version"],
            set_={"definition": rubric.raw, "active": True},
        )
        await session.execute(stmt)
    logger.info("Seeded active rubric {v}", v=rubric.version)
