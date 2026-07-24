"""Roster reconciliation from CRM + transcribed calls.

The scorer's content-identified manager name flows into ``scores`` and the
reconciled roster combines it (per-CRM-manager) with the authoritative Bitrix id.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.companion_user import CompanionUser
from AtamuraOKK.db.models.department import Department
from AtamuraOKK.db.models.enums import (
    CallDirection,
    CallStatus,
    CompanionRole,
)
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.db.models.score import Score
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.scoring.base import CallScore, CriterionScore
from AtamuraOKK.scoring.rubric import Rubric
from AtamuraOKK.scoring.rubric import load_rubric as _load_rubric
from AtamuraOKK.scoring.worker import _score_one
from AtamuraOKK.settings import settings
from AtamuraOKK.web.api.v1.auth import hash_key
from AtamuraOKK.web.api.v1.service import get_manager_roster

_TOKEN = "svc-token"  # noqa: S105
_ROSTER_URL = "/api/v1/managers/roster"


def _headers(user_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}", "X-Companion-User-Key": user_key}


@pytest.fixture
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "companion_api_token", _TOKEN)


async def _seed_key(
    session: AsyncSession,
    *,
    key: str,
    role: CompanionRole,
    bitrix_user_id: int | None = None,
    department_id: int | None = None,
) -> None:
    session.add(
        CompanionUser(
            key_sha256=hash_key(key),
            role=role,
            bitrix_user_id=bitrix_user_id,
            department_id=department_id,
            name="test",
        ),
    )
    await session.flush()


class _NamedScorer:
    """Reports a fixed spoken manager name on every scored call."""

    model_label = "fake/test"

    def __init__(self, spoken_name: str | None) -> None:
        self.spoken_name = spoken_name

    async def score(
        self,
        *,
        transcript: str,
        rubric: Rubric,
        direction: str,
        client_category: str | None = None,
    ) -> CallScore:
        return CallScore(
            call_type="квалификация",
            is_qualification_call=True,
            manager_identified=True,
            manager_spoken_name=self.spoken_name,
            criteria=[
                CriterionScore(
                    id=c.id,
                    score=c.max,
                    justification="ok",
                    evidence="",
                    recommendation="-",
                )
                for c in rubric.scored_criteria
            ],
            objections_present=False,
            sentiment_customer="нейтральный",
            sentiment_agent="нейтральный",
            summary="тест",
            red_flags=[],
            target_status="неясно",
            strengths="-",
            growth_zone="-",
            training_recommendation="-",
        )


async def _score_call(
    session: AsyncSession,
    manager: Manager,
    bitrix_call_id: str,
    spoken_name: str | None,
) -> None:
    call = Call(
        bitrix_call_id=bitrix_call_id,
        status=CallStatus.SCORING,
        direction=CallDirection.OUTBOUND,
        analyzable=True,
        manager_id=manager.id,
    )
    session.add(call)
    await session.flush()
    transcript = Transcript(call_id=call.id, full_text="[AGENT] привет")
    await _score_one(
        session, call, transcript, _NamedScorer(spoken_name), _load_rubric()
    )


async def test_spoken_name_persisted_on_score(dbsession: AsyncSession) -> None:
    """The scorer's ``manager_spoken_name`` lands on the score row."""
    manager = Manager(bitrix_user_id=9001, name="Айгуль", last_name="Сериковна")
    dbsession.add(manager)
    await dbsession.flush()

    await _score_call(dbsession, manager, "roster-1", "Айгуль")

    row = await dbsession.scalar(select(Score))
    assert row is not None
    assert row.manager_spoken_name == "Айгуль"


async def test_roster_combines_crm_and_spoken_names(dbsession: AsyncSession) -> None:
    """One CRM manager, several calls: roster tallies the distinct voiced names."""
    manager = Manager(bitrix_user_id=9002)  # deliberately un-enriched (no CRM name)
    dbsession.add(manager)
    await dbsession.flush()

    await _score_call(dbsession, manager, "roster-2a", "Айгуль")
    await _score_call(dbsession, manager, "roster-2b", "Айгуль")
    await _score_call(dbsession, manager, "roster-2c", "Aigul")
    await _score_call(dbsession, manager, "roster-2d", None)  # no name voiced

    roster = await get_manager_roster(dbsession)
    entry = next(e for e in roster if e.bitrix_user_id == 9002)

    assert entry.crm_name is None  # CRM stays authoritative — never invented
    # Distinct spoken names, most-frequent first, with per-name call counts.
    assert [(s.name, s.calls) for s in entry.spoken_names] == [
        ("Айгуль", 2),
        ("Aigul", 1),
    ]


async def test_roster_endpoint_head_sees_all(
    client: AsyncClient,
    dbsession: AsyncSession,
    _token: None,
) -> None:
    """Global head gets every manager, spoken names attached."""
    await _seed_key(dbsession, key="head", role=CompanionRole.HEAD)
    manager = Manager(bitrix_user_id=9101, name="Асхат", last_name="Мырзакулов")
    dbsession.add(manager)
    await dbsession.flush()
    await _score_call(dbsession, manager, "roster-ep-1", "Асхат")

    resp = await client.get(_ROSTER_URL, headers=_headers("head"))

    assert resp.status_code == 200
    entry = next(e for e in resp.json() if e["bitrix_user_id"] == 9101)
    assert entry["crm_name"] == "Асхат Мырзакулов"
    assert entry["spoken_names"] == [{"name": "Асхат", "calls": 1}]


async def test_roster_endpoint_forbidden_for_manager(
    client: AsyncClient,
    dbsession: AsyncSession,
    _token: None,
) -> None:
    """A manager key is 403 — the roster is a head-only view."""
    await _seed_key(
        dbsession,
        key="mgr",
        role=CompanionRole.MANAGER,
        bitrix_user_id=9102,
    )
    resp = await client.get(_ROSTER_URL, headers=_headers("mgr"))
    assert resp.status_code == 403


async def test_roster_endpoint_scoped_head_sees_only_department(
    client: AsyncClient,
    dbsession: AsyncSession,
    _token: None,
) -> None:
    """An office РОП sees only their own department's managers."""
    dept_a = Department(bitrix_id=1964, name="Keruen")
    dept_b = Department(bitrix_id=1970, name="Other")
    dbsession.add_all([dept_a, dept_b])
    await dbsession.flush()

    mine = Manager(bitrix_user_id=9201, name="Мой", department_id=dept_a.id)
    theirs = Manager(bitrix_user_id=9202, name="Чужой", department_id=dept_b.id)
    dbsession.add_all([mine, theirs])
    await dbsession.flush()

    # Scoped head keyed to dept_a's *Bitrix* id.
    await _seed_key(
        dbsession,
        key="roped",
        role=CompanionRole.HEAD,
        department_id=1964,
    )
    resp = await client.get(_ROSTER_URL, headers=_headers("roped"))

    assert resp.status_code == 200
    ids = {e["bitrix_user_id"] for e in resp.json()}
    assert ids == {9201}
