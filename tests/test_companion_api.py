"""Companion read API (/api/v1) — the sales-companion integration contract.

Verifies the anti-corruption read layer over call_scores_latest: ОКК 1–5
mapping, the call feed, per-call авто-разбор, the РОП team rollup, bearer-token
auth (incl. fail-closed when unset), and not-found / bad-period handling.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.department import Department
from AtamuraOKK.db.models.enums import CallDirection, CallStatus
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.db.models.score import Score
from AtamuraOKK.settings import settings

pytestmark = pytest.mark.anyio

_TOKEN = "test-companion-token"
_TZ = ZoneInfo(settings.report_timezone)
_PERIOD = "2026-03"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "companion_api_token", _TOKEN)


def _criteria_payload(percent: float, *, is_qual: bool = True) -> dict[str, Any]:
    return {
        "per_criterion": [
            {
                "id": 1,
                "block_id": "B1",
                "block_name": "Установление контакта",
                "text": "Поздоровался",
                "score": 4,
                "max": 5,
                "justification": "ок",
                "evidence": "Здравствуйте",
            },
        ],
        "blocks": {"B1": {"name": "Установление контакта", "score": 4, "max": 5}},
        "raw_points": 4,
        "max_points": 5,
        "percent": percent,
        "zone": "strong" if percent >= 85 else "risk",
        "call_type": "квалификация" if is_qual else "напоминание",
        "is_qualification_call": is_qual,
        "manager_identified": True,
        "objections_present": True,
        "target_status": "целевой",
        "strengths": "Хороший контакт",
        "growth_zone": "Работа с возражениями",
        "training_recommendation": "Тренинг по СПИН",
    }


async def _seed_scored_call(
    session: AsyncSession,
    *,
    bitrix_call_id: str,
    manager: Manager,
    percent: float,
    day: int = 15,
    is_qual: bool = True,
) -> Call:
    call = Call(
        bitrix_call_id=bitrix_call_id,
        portal_user_id=manager.bitrix_user_id,
        manager_id=manager.id,
        direction=CallDirection.OUTBOUND,
        started_at=datetime(2026, 3, day, 12, 0, tzinfo=_TZ),
        duration_sec=120,
        status=CallStatus.SCORED,
    )
    session.add(call)
    await session.flush()
    session.add(
        Score(
            call_id=call.id,
            rubric_version="okk-1",
            total_score=percent,
            criteria=_criteria_payload(percent, is_qual=is_qual),
            sentiment={"customer": "позитивный", "agent": "нейтральный"},
            summary="Клиент заинтересован, записан на встречу.",
            flags=[],
            model="fake/test",
        ),
    )
    await session.flush()
    return call


async def _seed_manager(
    session: AsyncSession,
    *,
    bitrix_user_id: int,
    department: Department | None = None,
    name: str = "Иван",
    last_name: str = "Петров",
) -> Manager:
    mgr = Manager(
        bitrix_user_id=bitrix_user_id,
        name=name,
        last_name=last_name,
        department_id=department.id if department else None,
    )
    session.add(mgr)
    await session.flush()
    return mgr


# --- auth -----------------------------------------------------------------


async def test_requires_token(client: AsyncClient, _token: None) -> None:
    """A valid bearer token is required."""
    resp = await client.get("/api/v1/managers/1/scorecard")
    assert resp.status_code == 401


async def test_fails_closed_when_token_unset(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no token configured the API fails closed (503)."""
    monkeypatch.setattr(settings, "companion_api_token", "")
    resp = await client.get("/api/v1/managers/1/scorecard", headers=_AUTH)
    assert resp.status_code == 503


# --- scorecard ------------------------------------------------------------


async def test_scorecard_maps_okk_1_to_5(
    client: AsyncClient,
    dbsession: AsyncSession,
    _token: None,
) -> None:
    """Average percent maps to the 1–5 ОКК modifier and zone."""
    mgr = await _seed_manager(dbsession, bitrix_user_id=501)
    await _seed_scored_call(dbsession, bitrix_call_id="c1", manager=mgr, percent=92.0)
    await _seed_scored_call(dbsession, bitrix_call_id="c2", manager=mgr, percent=88.0)

    resp = await client.get(
        f"/api/v1/managers/{mgr.bitrix_user_id}/scorecard?period={_PERIOD}",
        headers=_AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["manager"]["bitrix_user_id"] == 501
    assert body["calls_scored"] == 2
    assert body["okk"]["percent"] == 90.0  # avg(92, 88)
    assert body["okk"]["score_5"] == 5  # >= 90
    assert body["okk"]["zone"] == "strong"
    assert body["zone_distribution"]["strong"] == 2
    # Money axis is published but not wired in Phase 1.
    assert body["money"]["status"] == "not_available"
    assert body["money"]["conversion_pct"] is None


async def test_scorecard_excludes_non_qualification(
    client: AsyncClient,
    dbsession: AsyncSession,
    _token: None,
) -> None:
    """Non-qualification calls don't count toward the score."""
    mgr = await _seed_manager(dbsession, bitrix_user_id=502)
    await _seed_scored_call(dbsession, bitrix_call_id="q1", manager=mgr, percent=80.0)
    await _seed_scored_call(
        dbsession,
        bitrix_call_id="r1",
        manager=mgr,
        percent=10.0,
        is_qual=False,
    )

    resp = await client.get(
        f"/api/v1/managers/502/scorecard?period={_PERIOD}",
        headers=_AUTH,
    )
    body = resp.json()
    assert body["calls_scored"] == 1  # reminder excluded
    assert body["okk"]["percent"] == 80.0
    assert body["okk"]["score_5"] == 3


async def test_scorecard_unknown_manager_404(
    client: AsyncClient,
    _token: None,
) -> None:
    """An unknown Bitrix user id returns 404."""
    resp = await client.get("/api/v1/managers/999999/scorecard", headers=_AUTH)
    assert resp.status_code == 404


async def test_scorecard_bad_period_422(
    client: AsyncClient,
    dbsession: AsyncSession,
    _token: None,
) -> None:
    """A malformed period returns 422."""
    await _seed_manager(dbsession, bitrix_user_id=503)
    resp = await client.get(
        "/api/v1/managers/503/scorecard?period=2026-13",
        headers=_AUTH,
    )
    assert resp.status_code == 422


# --- calls feed + feedback ------------------------------------------------


async def test_calls_feed_and_feedback(
    client: AsyncClient,
    dbsession: AsyncSession,
    _token: None,
) -> None:
    """The call feed and per-call авто-разбор expose the scored fields."""
    mgr = await _seed_manager(dbsession, bitrix_user_id=504)
    call = await _seed_scored_call(
        dbsession,
        bitrix_call_id="feed1",
        manager=mgr,
        percent=86.0,
    )

    feed = await client.get("/api/v1/managers/504/calls", headers=_AUTH)
    assert feed.status_code == 200
    items = feed.json()
    assert len(items) == 1
    assert items[0]["call_id"] == call.id
    assert items[0]["okk_5"] == 4  # 86 -> strong band
    assert items[0]["summary"]

    detail = await client.get(f"/api/v1/calls/{call.id}/feedback", headers=_AUTH)
    assert detail.status_code == 200
    fb = detail.json()
    assert fb["strengths"] == "Хороший контакт"
    assert fb["training_recommendation"] == "Тренинг по СПИН"
    assert fb["sentiment_customer"] == "позитивный"
    assert len(fb["criteria"]) == 1
    assert fb["criteria"][0]["percent_of_max"] == 80.0


async def test_feedback_unknown_call_404(
    client: AsyncClient,
    _token: None,
) -> None:
    """Feedback for an unscored call returns 404."""
    resp = await client.get("/api/v1/calls/424242/feedback", headers=_AUTH)
    assert resp.status_code == 404


# --- team summary ---------------------------------------------------------


async def test_team_summary_rollup(
    client: AsyncClient,
    dbsession: AsyncSession,
    _token: None,
) -> None:
    """РОП-вид rolls up per-manager scorecards and the group average."""
    dept = Department(bitrix_id=77, name="Отдел ТМ")
    dbsession.add(dept)
    await dbsession.flush()
    a = await _seed_manager(dbsession, bitrix_user_id=601, department=dept)
    b = await _seed_manager(dbsession, bitrix_user_id=602, department=dept)
    await _seed_scored_call(dbsession, bitrix_call_id="t1", manager=a, percent=90.0)
    await _seed_scored_call(dbsession, bitrix_call_id="t2", manager=b, percent=70.0)

    resp = await client.get(
        f"/api/v1/teams/77/summary?period={_PERIOD}",
        headers=_AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["department"]["bitrix_id"] == 77
    assert body["group"]["calls_scored"] == 2
    assert body["group"]["okk"]["percent"] == 80.0  # avg(90, 70)
    assert len(body["roster"]) == 2
    # Roster sorted best-first.
    assert body["roster"][0]["manager"]["bitrix_user_id"] == 601
    assert body["roster"][0]["okk"]["score_5"] == 5
