"""Per-source rubric seeding: one active rubric per department axis.

``seed_active_rubrics`` publishes the ТМ call rubric (source "tm") and the
ОП meeting rubric (source "op") into ``rubric_versions``; the partial unique
index keeps exactly one active row per source.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.models.rubric_version import RubricVersion
from AtamuraOKK.scoring.seed import _seed_one, seed_active_rubrics

pytestmark = pytest.mark.anyio


async def _active_by_source(session: AsyncSession) -> dict[str, str]:
    rows = (
        await session.scalars(select(RubricVersion).where(RubricVersion.active))
    ).all()
    return {r.source: r.version for r in rows}


async def test_seeds_one_active_rubric_per_source(dbsession: AsyncSession) -> None:
    """Both production rubrics land, each its source's single active row."""
    await seed_active_rubrics(session=dbsession)
    assert await _active_by_source(dbsession) == {
        "tm": "tm-call-v2",
        "op": "okk_meeting_v1",
    }


async def test_seed_is_idempotent(dbsession: AsyncSession) -> None:
    """Re-running refreshes in place — still one active row per source."""
    await seed_active_rubrics(session=dbsession)
    await seed_active_rubrics(session=dbsession)
    rows = (await dbsession.scalars(select(RubricVersion))).all()
    assert len(rows) == 2
    assert all(r.active for r in rows)


async def test_new_version_deactivates_old_within_source(
    dbsession: AsyncSession,
) -> None:
    """Activating a new version retires only its own source's predecessor."""
    await seed_active_rubrics(session=dbsession)
    await _seed_one(
        dbsession,
        source="op",
        version="okk_meeting_v2",
        definition={"id": "okk_meeting_v2", "criteria": []},
    )
    assert await _active_by_source(dbsession) == {
        "tm": "tm-call-v2",
        "op": "okk_meeting_v2",
    }


async def test_second_active_row_per_source_rejected(
    dbsession: AsyncSession,
) -> None:
    """The partial unique index forbids two active rubrics for one source."""
    dbsession.add(RubricVersion(source="tm", version="v1", definition={}, active=True))
    await dbsession.flush()
    # Savepoint keeps the constraint violation from poisoning the test session.
    with pytest.raises(IntegrityError):
        async with dbsession.begin_nested():
            dbsession.add(
                RubricVersion(source="tm", version="v2", definition={}, active=True),
            )
            await dbsession.flush()
