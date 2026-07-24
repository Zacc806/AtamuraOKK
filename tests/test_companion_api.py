"""Companion read API (/api/v1) — the sales-companion integration contract.

Verifies the anti-corruption read layer over call_scores_latest: ОКК 1–5
mapping, the call feed, per-call авто-разбор, the РОП team rollup, two-layer
auth (service bearer incl. fail-closed when unset + personal user keys), the
manager/head role scoping, the static РОП key + head-tiered /users access
management (the global head mints manager and scoped-head keys; an office
РОП manages their own department's manager keys, which tie the manager to
that department), and not-found / bad-period handling.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any, Self
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.bitrix import BitrixError
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.companion_user import CompanionUser
from AtamuraOKK.db.models.department import Department
from AtamuraOKK.db.models.enums import CallDirection, CallStatus, CompanionRole
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.db.models.meeting import Meeting
from AtamuraOKK.db.models.rubric_version import RubricVersion
from AtamuraOKK.db.models.score import Score
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.settings import settings
from AtamuraOKK.web.api.v1 import service
from AtamuraOKK.web.api.v1.auth import hash_key

pytestmark = pytest.mark.anyio

_TOKEN = "test-companion-token"
_TZ = ZoneInfo(settings.report_timezone)
_PERIOD = "2026-03"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_HEAD_KEY = "head-personal-key"
_MANAGER_KEY = "manager-personal-key"


def _headers(user_key: str) -> dict[str, str]:
    return {**_AUTH, "X-Companion-User-Key": user_key}


@pytest.fixture
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "companion_api_token", _TOKEN)


async def _seed_companion_user(
    session: AsyncSession,
    *,
    key: str,
    role: CompanionRole,
    bitrix_user_id: int | None = None,
    department_id: int | None = None,
    name: str = "Пользователь",
) -> CompanionUser:
    user = CompanionUser(
        key_sha256=hash_key(key),
        role=role,
        bitrix_user_id=bitrix_user_id,
        department_id=department_id,
        name=name,
    )
    session.add(user)
    await session.flush()
    return user


@pytest.fixture
async def head_auth(dbsession: AsyncSession, _token: None) -> dict[str, str]:
    """Headers for a head-of-sales session (sees everything)."""
    await _seed_companion_user(
        dbsession,
        key=_HEAD_KEY,
        role=CompanionRole.HEAD,
        name="РОП",
    )
    return _headers(_HEAD_KEY)


def _criteria_payload(
    percent: float, *, is_qual: bool = True, target_status: str = "целевой"
) -> dict[str, Any]:
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
        "target_status": target_status,
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
    target_status: str = "целевой",
    started_at: datetime | None = None,
    crm_entity_type: str | None = None,
    crm_entity_id: int | None = None,
) -> Call:
    call = Call(
        bitrix_call_id=bitrix_call_id,
        portal_user_id=manager.bitrix_user_id,
        manager_id=manager.id,
        direction=CallDirection.OUTBOUND,
        started_at=started_at or datetime(2026, 3, day, 12, 0, tzinfo=_TZ),
        duration_sec=120,
        status=CallStatus.SCORED,
        crm_entity_type=crm_entity_type,
        crm_entity_id=crm_entity_id,
    )
    session.add(call)
    await session.flush()
    session.add(
        Score(
            call_id=call.id,
            rubric_version="okk-1",
            total_score=percent,
            criteria=_criteria_payload(
                percent, is_qual=is_qual, target_status=target_status
            ),
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
    head_auth: dict[str, str],
) -> None:
    """Average percent maps to the 1–5 ОКК modifier and zone."""
    mgr = await _seed_manager(dbsession, bitrix_user_id=501)
    await _seed_scored_call(dbsession, bitrix_call_id="c1", manager=mgr, percent=92.0)
    await _seed_scored_call(dbsession, bitrix_call_id="c2", manager=mgr, percent=88.0)

    resp = await client.get(
        f"/api/v1/managers/{mgr.bitrix_user_id}/scorecard?period={_PERIOD}",
        headers=head_auth,
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
    head_auth: dict[str, str],
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
        headers=head_auth,
    )
    body = resp.json()
    assert body["calls_scored"] == 1  # reminder excluded
    assert body["okk"]["percent"] == 80.0
    assert body["okk"]["score_5"] == 3


async def test_scorecard_counts_non_target_qualification(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
) -> None:
    """target_status doesn't gate the score; only non-qualification calls drop.

    A real qualification call counts even when the client was judged нецелевой /
    неясно — target_status is informational, not a score gate.
    """
    mgr = await _seed_manager(dbsession, bitrix_user_id=505)
    await _seed_scored_call(dbsession, bitrix_call_id="t1", manager=mgr, percent=90.0)
    # Non-target but still a real client call → counts.
    await _seed_scored_call(
        dbsession,
        bitrix_call_id="n1",
        manager=mgr,
        percent=70.0,
        target_status="нецелевой",
    )
    await _seed_scored_call(
        dbsession,
        bitrix_call_id="u1",
        manager=mgr,
        percent=80.0,
        target_status="неясно",
    )
    # Not a qualification call (realtor/vendor/etc.) → excluded.
    await _seed_scored_call(
        dbsession,
        bitrix_call_id="x1",
        manager=mgr,
        percent=10.0,
        is_qual=False,
    )

    resp = await client.get(
        f"/api/v1/managers/505/scorecard?period={_PERIOD}",
        headers=head_auth,
    )
    body = resp.json()
    assert body["calls_scored"] == 3  # all three qualification calls count
    assert body["okk"]["percent"] == 80.0  # (90 + 70 + 80) / 3


async def test_criteria_averages(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
) -> None:
    """Балл ОКК averages each criterion over qual calls; non-qual skipped."""
    mgr = await _seed_manager(dbsession, bitrix_user_id=520)
    # criterion 1 scores 4/5 on each qual call → avg 4, 80%; reminder ignored.
    await _seed_scored_call(dbsession, bitrix_call_id="a", manager=mgr, percent=80.0)
    await _seed_scored_call(dbsession, bitrix_call_id="b", manager=mgr, percent=80.0)
    # Non-target but still a qualification call → counts.
    await _seed_scored_call(
        dbsession, bitrix_call_id="c", manager=mgr, percent=10.0,
        target_status="нецелевой",
    )
    await _seed_scored_call(
        dbsession, bitrix_call_id="d", manager=mgr, percent=10.0, is_qual=False,
    )

    resp = await client.get(
        f"/api/v1/managers/520/criteria?period={_PERIOD}",
        headers=head_auth,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["calls_scored"] == 3
    crit = {c["criterion_id"]: c for c in body["criteria"]}
    assert crit[1]["count"] == 3
    assert crit[1]["avg_score"] == 4.0
    assert crit[1]["avg_pct_of_max"] == 80.0
    assert crit[1]["max"] == 5.0


async def test_score_trend_buckets_recent_calls(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
) -> None:
    """Динамика buckets qualification calls by day over the trailing window."""
    mgr = await _seed_manager(dbsession, bitrix_user_id=521)
    today = datetime.now(tz=_TZ).replace(hour=12, minute=0, second=0, microsecond=0)
    await _seed_scored_call(
        dbsession, bitrix_call_id="t1", manager=mgr, percent=80.0, started_at=today,
    )
    await _seed_scored_call(
        dbsession, bitrix_call_id="t2", manager=mgr, percent=60.0, started_at=today,
    )
    # Non-qualification call → excluded from the trend.
    await _seed_scored_call(
        dbsession, bitrix_call_id="t3", manager=mgr, percent=10.0,
        is_qual=False, started_at=today,
    )

    resp = await client.get(
        "/api/v1/managers/521/score-trend?bucket=day",
        headers=head_auth,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["bucket"] == "day"
    point = {p["bucket"]: p for p in body["points"]}[today.date().isoformat()]
    assert point["calls"] == 2  # non-qualification excluded
    assert point["avg_percent"] == 70.0  # (80 + 60) / 2


async def test_score_trend_anchor_scrolls_window(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
) -> None:
    """?anchor= ends the trailing window on a past day, surfacing older calls."""
    mgr = await _seed_manager(dbsession, bitrix_user_id=523)
    old = datetime.now(tz=_TZ).replace(
        hour=12, minute=0, second=0, microsecond=0,
    ) - timedelta(days=40)
    await _seed_scored_call(
        dbsession, bitrix_call_id="old1", manager=mgr, percent=90.0, started_at=old,
    )
    key = old.date().isoformat()

    # Default (today) window is the last 14 days — the 40-day-old call is absent.
    now_resp = await client.get(
        "/api/v1/managers/523/score-trend?bucket=day", headers=head_auth,
    )
    assert key not in {p["bucket"] for p in now_resp.json()["points"]}

    # Anchored on the old day, it falls inside the window.
    resp = await client.get(
        f"/api/v1/managers/523/score-trend?bucket=day&anchor={key}", headers=head_auth,
    )
    assert resp.status_code == 200
    point = {p["bucket"]: p for p in resp.json()["points"]}[key]
    assert point["calls"] == 1
    assert point["avg_percent"] == 90.0


async def test_score_trend_bad_anchor_422(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
) -> None:
    """A non-day anchor (month/range/garbage) is a 422."""
    await _seed_manager(dbsession, bitrix_user_id=524)
    resp = await client.get(
        "/api/v1/managers/524/score-trend?bucket=day&anchor=2026-06", headers=head_auth,
    )
    assert resp.status_code == 422


async def test_score_trend_bad_bucket_422(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
) -> None:
    """An unknown bucket granularity returns 422."""
    await _seed_manager(dbsession, bitrix_user_id=522)
    resp = await client.get(
        "/api/v1/managers/522/score-trend?bucket=hour",
        headers=head_auth,
    )
    assert resp.status_code == 422


async def test_scorecard_unknown_manager_404(
    client: AsyncClient,
    head_auth: dict[str, str],
) -> None:
    """An unknown Bitrix user id returns 404."""
    resp = await client.get("/api/v1/managers/999999/scorecard", headers=head_auth)
    assert resp.status_code == 404


async def test_scorecard_bad_period_422(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
) -> None:
    """A malformed period returns 422."""
    await _seed_manager(dbsession, bitrix_user_id=503)
    resp = await client.get(
        "/api/v1/managers/503/scorecard?period=2026-13",
        headers=head_auth,
    )
    assert resp.status_code == 422


# --- calls feed + feedback ------------------------------------------------


async def test_calls_feed_and_feedback(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The call feed and per-call авто-разбор expose the scored fields."""
    monkeypatch.setattr(
        settings,
        "bitrix_webhook",
        "https://portal.bitrix24.kz/rest/1/tok/",
    )
    mgr = await _seed_manager(dbsession, bitrix_user_id=504)
    call = await _seed_scored_call(
        dbsession,
        bitrix_call_id="feed1",
        manager=mgr,
        percent=86.0,
        crm_entity_type="DEAL",
        crm_entity_id=777,
    )
    dbsession.add(
        Transcript(
            call_id=call.id,
            language="ru",
            full_text="[AGENT]\nДобрый день!\n\n[CUSTOMER]\nЗдравствуйте.",
            segments=[
                {"speaker": "agent", "start": 0.0, "end": 0.0, "text": "Добрый "},
                {"speaker": "agent", "start": 0.0, "end": 0.0, "text": "день!"},
                {
                    "speaker": "customer",
                    "start": 0.0,
                    "end": 0.0,
                    "text": "Здравствуйте.",
                },
            ],
        ),
    )
    await dbsession.flush()

    feed = await client.get("/api/v1/managers/504/calls", headers=head_auth)
    assert feed.status_code == 200
    items = feed.json()
    assert len(items) == 1
    assert items[0]["call_id"] == call.id
    assert items[0]["okk_5"] == 4  # 86 -> strong band
    assert items[0]["summary"]
    assert items[0]["bitrix_url"] == "https://portal.bitrix24.kz/crm/deal/details/777/"

    detail = await client.get(f"/api/v1/calls/{call.id}/feedback", headers=head_auth)
    assert detail.status_code == 200
    fb = detail.json()
    assert fb["strengths"] == "Хороший контакт"
    assert fb["training_recommendation"] == "Тренинг по СПИН"
    assert fb["sentiment_customer"] == "позитивный"
    assert fb["bitrix_url"] == "https://portal.bitrix24.kz/crm/deal/details/777/"
    assert len(fb["criteria"]) == 1
    assert fb["criteria"][0]["percent_of_max"] == 80.0
    # Consecutive same-speaker segments coalesce into one block per speaker.
    assert fb["transcript"] == [
        {"speaker": "agent", "text": "Добрый день!"},
        {"speaker": "customer", "text": "Здравствуйте."},
    ]


class _FakeCrmBitrix:
    """Stands in for BitrixClient in the CRM card → calls resolution.

    Replays the reads ``_crm_entity_pairs`` makes: ``crm.deal.get`` /
    ``crm.contact.get`` / ``crm.deal.contact.items.get`` via ``call``, and
    ``crm.deal.list`` / ``crm.contact.list`` via ``list``. Pass ``raises=True``
    to exercise the degrade-to-direct-entity path.
    """

    def __init__(
        self,
        *,
        deal: dict[str, Any] | None = None,
        contact: dict[str, Any] | None = None,
        contacts: list[dict[str, Any]] | None = None,
        deal_ids: list[int] | None = None,
        contact_ids: list[int] | None = None,
        raises: bool = False,
    ) -> None:
        self.deal = deal
        self.contact = contact
        self.contacts = contacts or []
        self.deal_ids = deal_ids or []
        self.contact_ids = contact_ids or []
        self.raises = raises

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self.raises:
            raise BitrixError("ERR", "boom", method)
        if method == "crm.deal.get":
            return self.deal
        if method == "crm.contact.get":
            return self.contact
        if method == "crm.deal.contact.items.get":
            return self.contacts
        raise AssertionError(f"unexpected method {method}")

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        if self.raises:
            raise BitrixError("ERR", "boom", method)
        rows = {
            "crm.deal.list": self.deal_ids,
            "crm.contact.list": self.contact_ids,
        }.get(method)
        if rows is None:
            raise AssertionError(f"unexpected list {method}")
        for row_id in rows:
            yield {"ID": str(row_id)}


def _patch_crm_bitrix(
    monkeypatch: pytest.MonkeyPatch,
    fake: _FakeCrmBitrix,
) -> None:
    monkeypatch.setattr(service, "BitrixClient", lambda *a, **k: fake)


async def test_deal_calls_resolved_via_contact(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deal's calls are found through its contact, not just the deal itself."""
    _patch_crm_bitrix(
        monkeypatch,
        _FakeCrmBitrix(deal={"CONTACT_ID": "123", "COMPANY_ID": "0"}),
    )
    mgr = await _seed_manager(dbsession, bitrix_user_id=520)
    via_contact = await _seed_scored_call(
        dbsession,
        bitrix_call_id="deal-c1",
        manager=mgr,
        percent=90.0,
        day=10,
        crm_entity_type="CONTACT",
        crm_entity_id=123,
    )
    via_deal = await _seed_scored_call(
        dbsession,
        bitrix_call_id="deal-c2",
        manager=mgr,
        percent=70.0,
        day=12,
        crm_entity_type="DEAL",
        crm_entity_id=536096,
    )
    # A different contact's call must NOT come back.
    await _seed_scored_call(
        dbsession,
        bitrix_call_id="deal-c3",
        manager=mgr,
        percent=50.0,
        crm_entity_type="CONTACT",
        crm_entity_id=999,
    )

    resp = await client.get("/api/v1/deals/536096/calls", headers=head_auth)
    assert resp.status_code == 200
    ids = [item["call_id"] for item in resp.json()]
    # Both the contact-linked and the deal-linked call, newest first.
    assert ids == [via_deal.id, via_contact.id]


async def test_deal_calls_degrade_when_bitrix_down(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Bitrix failure still returns calls linked directly to the deal."""
    _patch_crm_bitrix(monkeypatch, _FakeCrmBitrix(raises=True))
    mgr = await _seed_manager(dbsession, bitrix_user_id=521)
    direct = await _seed_scored_call(
        dbsession,
        bitrix_call_id="deal-d1",
        manager=mgr,
        percent=80.0,
        crm_entity_type="DEAL",
        crm_entity_id=440000,
    )
    resp = await client.get("/api/v1/deals/440000/calls", headers=head_auth)
    assert resp.status_code == 200
    assert [item["call_id"] for item in resp.json()] == [direct.id]


async def test_deal_calls_scoped_to_manager(
    client: AsyncClient,
    dbsession: AsyncSession,
    manager_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manager sees only their own calls on a shared deal's contact."""
    _patch_crm_bitrix(
        monkeypatch,
        _FakeCrmBitrix(deal={"CONTACT_ID": "321", "COMPANY_ID": "0"}),
    )
    # manager_auth already seeded manager 701; reuse that row.
    me = (
        await dbsession.execute(select(Manager).where(Manager.bitrix_user_id == 701))
    ).scalar_one()
    other = await _seed_manager(dbsession, bitrix_user_id=702)
    mine = await _seed_scored_call(
        dbsession,
        bitrix_call_id="deal-m1",
        manager=me,
        percent=88.0,
        crm_entity_type="CONTACT",
        crm_entity_id=321,
    )
    await _seed_scored_call(
        dbsession,
        bitrix_call_id="deal-m2",
        manager=other,
        percent=88.0,
        crm_entity_type="CONTACT",
        crm_entity_id=321,
    )
    resp = await client.get("/api/v1/deals/536096/calls", headers=manager_auth)
    assert resp.status_code == 200
    assert [item["call_id"] for item in resp.json()] == [mine.id]


async def test_crm_contact_calls_direct_match(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contact card (where Bitrix lands on call-open) finds the contact's calls.

    Calls link to the contact directly, plus any linked via the contact's deal.
    """
    _patch_crm_bitrix(
        monkeypatch,
        _FakeCrmBitrix(contact={"COMPANY_ID": "0"}, deal_ids=[536096]),
    )
    mgr = await _seed_manager(dbsession, bitrix_user_id=530)
    on_contact = await _seed_scored_call(
        dbsession,
        bitrix_call_id="ct-1",
        manager=mgr,
        percent=91.0,
        day=14,
        crm_entity_type="CONTACT",
        crm_entity_id=429546,
    )
    on_contact_deal = await _seed_scored_call(
        dbsession,
        bitrix_call_id="ct-2",
        manager=mgr,
        percent=72.0,
        day=11,
        crm_entity_type="DEAL",
        crm_entity_id=536096,
    )
    await _seed_scored_call(
        dbsession,
        bitrix_call_id="ct-3",
        manager=mgr,
        percent=55.0,
        crm_entity_type="CONTACT",
        crm_entity_id=111,
    )

    resp = await client.get("/api/v1/crm/contact/429546/calls", headers=head_auth)
    assert resp.status_code == 200
    ids = [item["call_id"] for item in resp.json()]
    assert ids == [on_contact.id, on_contact_deal.id]


async def test_crm_contact_calls_degrade_when_bitrix_down(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with Bitrix down, a contact card still matches the contact directly."""
    _patch_crm_bitrix(monkeypatch, _FakeCrmBitrix(raises=True))
    mgr = await _seed_manager(dbsession, bitrix_user_id=531)
    direct = await _seed_scored_call(
        dbsession,
        bitrix_call_id="ct-d1",
        manager=mgr,
        percent=83.0,
        crm_entity_type="CONTACT",
        crm_entity_id=700700,
    )
    resp = await client.get("/api/v1/crm/contact/700700/calls", headers=head_auth)
    assert resp.status_code == 200
    assert [item["call_id"] for item in resp.json()] == [direct.id]


async def test_crm_unknown_entity_type_404(
    client: AsyncClient,
    head_auth: dict[str, str],
) -> None:
    """An unsupported CRM entity type is rejected."""
    resp = await client.get("/api/v1/crm/invoice/123/calls", headers=head_auth)
    assert resp.status_code == 404


async def test_feedback_unknown_call_404(
    client: AsyncClient,
    head_auth: dict[str, str],
) -> None:
    """Feedback for an unscored call returns 404."""
    resp = await client.get("/api/v1/calls/424242/feedback", headers=head_auth)
    assert resp.status_code == 404


# --- meetings feed + feedback (ОП) ------------------------------------------


async def _seed_meeting(
    session: AsyncSession,
    *,
    bitrix_file_id: int,
    uploaded_by: int | None,
    manager: Manager | None = None,
    percent: float = 82.0,
    day: int = 15,
    needs_review: bool = False,
) -> Meeting:
    meeting = Meeting(
        bitrix_file_id=bitrix_file_id,
        name=f"WhatsApp Audio 2026-03-{day:02d} at 12.00.00.mp4",
        folder_path="Встречи ОП/Март",
        source="op",
        uploaded_by_bitrix_id=uploaded_by,
        manager_id=manager.id if manager else None,
        meeting_at=datetime(2026, 3, day, 12, 0, tzinfo=_TZ),
        duration_sec=1800,
        language="ru",
        rubric_version="okk_meeting_v1",
        score_pct=percent,
        passed=percent >= 75,
        call_type="первичный",
        manager_tone="вежливый",
        needs_human_review=needs_review,
        summary="Клиент выбрал планировку, ждёт расчёт.",
        red_flags=["обещал скидку без согласования"],
        score={
            "criteria": [
                {
                    "id": 1,
                    "block": "Контакт",
                    "name": "Приветствие",
                    "score": 4,
                    "max_score": 5,
                    "auto": False,
                },
            ],
            "script_adherence": 70.0,
            "script_deviations": ["пропустил презентацию ЖК"],
        },
    )
    session.add(meeting)
    await session.flush()
    return meeting


async def test_meetings_feed_and_feedback(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
) -> None:
    """The meetings feed + авто-разбор expose the scored ОП-meeting fields."""
    mgr = await _seed_manager(dbsession, bitrix_user_id=505)
    older = await _seed_meeting(
        dbsession,
        bitrix_file_id=9001,
        uploaded_by=505,
        manager=mgr,
        day=10,
    )
    newest = await _seed_meeting(
        dbsession,
        bitrix_file_id=9002,
        uploaded_by=505,
        manager=mgr,
        percent=64.0,
        day=20,
    )

    feed = await client.get("/api/v1/managers/505/meetings", headers=head_auth)
    assert feed.status_code == 200
    items = feed.json()
    assert [i["meeting_id"] for i in items] == [newest.id, older.id]  # newest first
    assert items[0]["percent"] == 64.0
    assert items[0]["passed"] is False
    assert items[0]["source"] == "op"
    assert items[0]["red_flags"] == ["обещал скидку без согласования"]

    detail = await client.get(
        f"/api/v1/meetings/{older.id}/feedback",
        headers=head_auth,
    )
    assert detail.status_code == 200
    fb = detail.json()
    assert fb["manager"]["bitrix_user_id"] == 505
    assert fb["percent"] == 82.0
    assert fb["script_adherence"] == 70.0
    assert fb["script_deviations"] == ["пропустил презентацию ЖК"]
    assert len(fb["criteria"]) == 1
    assert fb["criteria"][0]["name"] == "Приветствие"
    assert fb["criteria"][0]["max"] == 5.0


async def test_meeting_feedback_unknown_404(
    client: AsyncClient,
    head_auth: dict[str, str],
) -> None:
    """Feedback for an unknown meeting returns 404."""
    resp = await client.get("/api/v1/meetings/424242/feedback", headers=head_auth)
    assert resp.status_code == 404


# --- team summary ---------------------------------------------------------


async def test_team_summary_rollup(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
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
        headers=head_auth,
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


async def test_team_summary_counts_visit_conversions(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TM team view carries «Фактический визит» conversions per manager."""
    dept = Department(bitrix_id=settings.companion_tm_department_id, name="ТМ")
    dbsession.add(dept)
    await dbsession.flush()
    a = await _seed_manager(dbsession, bitrix_user_id=701, department=dept)
    b = await _seed_manager(dbsession, bitrix_user_id=702, department=dept)
    await _seed_scored_call(dbsession, bitrix_call_id="v1", manager=a, percent=90.0)
    await _seed_scored_call(dbsession, bitrix_call_id="v2", manager=b, percent=70.0)

    # Bitrix-derived visit counts (one stage-history pull, keyed by TM user id).
    async def _fake_visits(_start: datetime, _end: datetime) -> dict[int, int]:
        return {701: 7, 702: 3}

    monkeypatch.setattr(service, "_visits_by_tm", _fake_visits)

    resp = await client.get(
        f"/api/v1/teams/{settings.companion_tm_department_id}/summary?period={_PERIOD}",
        headers=head_auth,
    )
    assert resp.status_code == 200
    body = resp.json()
    by_uid = {m["manager"]["bitrix_user_id"]: m for m in body["roster"]}
    assert by_uid[701]["money"]["meetings"] == 7
    assert by_uid[702]["money"]["meetings"] == 3
    assert body["group"]["money"]["meetings"] == 10  # team total
    assert body["group"]["money"]["status"] == "live"


async def test_team_summary_visits_absent_when_bitrix_unavailable(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bitrix down → visit counts are simply omitted, the rollup still serves."""
    dept = Department(bitrix_id=settings.companion_tm_department_id, name="ТМ")
    dbsession.add(dept)
    await dbsession.flush()
    a = await _seed_manager(dbsession, bitrix_user_id=703, department=dept)
    await _seed_scored_call(dbsession, bitrix_call_id="v3", manager=a, percent=88.0)

    async def _no_visits(_start: datetime, _end: datetime) -> dict[int, int]:
        return {}

    monkeypatch.setattr(service, "_visits_by_tm", _no_visits)

    resp = await client.get(
        f"/api/v1/teams/{settings.companion_tm_department_id}/summary?period={_PERIOD}",
        headers=head_auth,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["roster"][0]["money"]["meetings"] is None
    assert body["group"]["money"]["meetings"] is None


# --- meetings in scorecard / team summary (per-department items) ------------


async def test_scorecard_includes_meetings_aggregate(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
) -> None:
    """An ОП manager's scorecard carries the meetings block (pct/pass)."""
    mgr = await _seed_manager(dbsession, bitrix_user_id=511)
    await _seed_meeting(
        dbsession,
        bitrix_file_id=9201,
        uploaded_by=511,
        manager=mgr,
        percent=90.0,
        day=5,
    )
    await _seed_meeting(
        dbsession,
        bitrix_file_id=9202,
        uploaded_by=511,
        manager=mgr,
        percent=60.0,
        day=6,
        needs_review=True,
    )

    resp = await client.get(
        f"/api/v1/managers/511/scorecard?period={_PERIOD}",
        headers=head_auth,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["calls_scored"] == 0
    meetings = body["meetings"]
    assert meetings["meetings_scored"] == 2
    assert meetings["avg_score_pct"] == 75.0
    assert meetings["passed"] == 1
    assert meetings["failed"] == 1
    assert meetings["needs_human_review"] == 1


async def test_scorecard_meetings_block_defaults_to_zero(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
) -> None:
    """A calls-only (ТМ) manager still gets the meetings block, zeroed."""
    mgr = await _seed_manager(dbsession, bitrix_user_id=512)
    await _seed_scored_call(dbsession, bitrix_call_id="tm1", manager=mgr, percent=90.0)

    resp = await client.get(
        f"/api/v1/managers/512/scorecard?period={_PERIOD}",
        headers=head_auth,
    )
    meetings = resp.json()["meetings"]
    assert meetings["meetings_scored"] == 0
    assert meetings["avg_score_pct"] is None


async def test_team_summary_includes_meetings(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
) -> None:
    """An ОП department rolls up its meetings; placeholder managers excluded."""
    dept = Department(bitrix_id=78, name="Отдел продаж")
    dbsession.add(dept)
    await dbsession.flush()
    caller = await _seed_manager(dbsession, bitrix_user_id=611, department=dept)
    meeter = await _seed_manager(dbsession, bitrix_user_id=612, department=dept)
    # Unenriched placeholder: no department yet, so invisible in the rollup.
    orphan = await _seed_manager(dbsession, bitrix_user_id=613)
    await _seed_scored_call(
        dbsession,
        bitrix_call_id="u1",
        manager=caller,
        percent=88.0,
    )
    await _seed_meeting(
        dbsession,
        bitrix_file_id=9301,
        uploaded_by=612,
        manager=meeter,
        percent=80.0,
    )
    await _seed_meeting(
        dbsession,
        bitrix_file_id=9302,
        uploaded_by=613,
        manager=orphan,
        percent=10.0,
    )

    resp = await client.get(
        f"/api/v1/teams/78/summary?period={_PERIOD}",
        headers=head_auth,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["group"]["calls_scored"] == 1
    assert body["group"]["meetings"]["meetings_scored"] == 1  # orphan excluded
    assert body["group"]["meetings"]["avg_score_pct"] == 80.0

    by_uid = {m["manager"]["bitrix_user_id"]: m for m in body["roster"]}
    assert set(by_uid) == {611, 612}  # union: call- and meeting-managers
    assert by_uid[611]["okk"]["percent"] == 88.0
    assert by_uid[612]["calls_scored"] == 0
    assert by_uid[612]["meetings"]["meetings_scored"] == 1
    # Sorted by primary score: 88 (calls) before 80 (meetings).
    assert [m["manager"]["bitrix_user_id"] for m in body["roster"]] == [611, 612]


# --- unified feed -----------------------------------------------------------


async def test_unified_feed_merges_calls_and_meetings(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
) -> None:
    """/feed interleaves kind-tagged calls + meetings, newest first."""
    mgr = await _seed_manager(dbsession, bitrix_user_id=513)
    await _seed_scored_call(
        dbsession,
        bitrix_call_id="f1",
        manager=mgr,
        percent=85.0,
        day=12,
    )
    await _seed_meeting(
        dbsession,
        bitrix_file_id=9401,
        uploaded_by=513,
        manager=mgr,
        day=15,
    )
    await _seed_scored_call(
        dbsession,
        bitrix_call_id="f2",
        manager=mgr,
        percent=70.0,
        day=20,
    )

    resp = await client.get("/api/v1/managers/513/feed", headers=head_auth)
    assert resp.status_code == 200
    items = resp.json()
    assert [i["kind"] for i in items] == ["call", "meeting", "call"]
    assert items[0]["call"]["bitrix_call_id"] == "f2"
    assert items[0]["meeting"] is None
    assert items[1]["meeting"]["bitrix_file_id"] == 9401

    truncated = await client.get(
        "/api/v1/managers/513/feed?limit=2",
        headers=head_auth,
    )
    assert [i["kind"] for i in truncated.json()] == ["call", "meeting"]


class _FakeContactBitrix:
    """Replays crm.contact.list for the feed's client-name resolution."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        assert method == "crm.contact.list"
        for row in self._rows:
            yield row


async def test_feed_labels_call_with_client_name(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contact-linked call is titled by the client's Bitrix name (not «целевой»)."""
    mgr = await _seed_manager(dbsession, bitrix_user_id=707)
    await _seed_scored_call(
        dbsession,
        bitrix_call_id="c1",
        manager=mgr,
        percent=80.0,
        crm_entity_type="CONTACT",
        crm_entity_id=4242,
    )
    fake = _FakeContactBitrix(
        [
            {
                "ID": "4242",
                "NAME": "Айгуль",
                "LAST_NAME": "Сатпаева",
                "PHONE": [{"VALUE": "+77011234567"}],
            },
        ],
    )
    monkeypatch.setattr(service, "BitrixClient", lambda *a, **k: fake)

    resp = await client.get("/api/v1/managers/707/feed", headers=head_auth)
    assert resp.status_code == 200
    call = next(i["call"] for i in resp.json() if i["kind"] == "call")
    assert call["client_name"] == "Айгуль Сатпаева"
    assert call["phone"] == "+77011234567"


async def test_feed_falls_back_to_phone_then_none(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No contact name → phone; a non-contact call → neither (UI uses the label)."""
    mgr = await _seed_manager(dbsession, bitrix_user_id=708)
    await _seed_scored_call(
        dbsession,
        bitrix_call_id="c-phone",
        manager=mgr,
        percent=75.0,
        day=10,
        crm_entity_type="CONTACT",
        crm_entity_id=4243,
    )
    await _seed_scored_call(
        dbsession,
        bitrix_call_id="c-deal",
        manager=mgr,
        percent=65.0,
        day=20,
        crm_entity_type="DEAL",
        crm_entity_id=999,
    )
    fake = _FakeContactBitrix(
        [{"ID": "4243", "PHONE": [{"VALUE": "+77015550000"}]}],  # no name
    )
    monkeypatch.setattr(service, "BitrixClient", lambda *a, **k: fake)

    resp = await client.get("/api/v1/managers/708/feed", headers=head_auth)
    calls = {i["call"]["bitrix_call_id"]: i["call"] for i in resp.json()}
    assert calls["c-phone"]["client_name"] is None
    assert calls["c-phone"]["phone"] == "+77015550000"
    assert calls["c-deal"]["client_name"] is None  # DEAL isn't name-resolved
    assert calls["c-deal"]["phone"] is None


async def test_unified_feed_is_scoped(
    client: AsyncClient,
    manager_auth: dict[str, str],
) -> None:
    """A manager cannot read another manager's unified feed."""
    resp = await client.get("/api/v1/managers/999/feed", headers=manager_auth)
    assert resp.status_code == 403


# --- active rubrics (per-department criteria) --------------------------------


async def test_rubrics_returns_active_criteria_per_source(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
) -> None:
    """GET /rubrics normalizes both rubric shapes; crm criteria excluded."""
    dbsession.add(
        RubricVersion(
            source="tm",
            version="tm-call-v2",
            active=True,
            definition={
                "version": "tm-call-v2",
                "name": "Чек-лист ТМ",
                "zones": {"strong": 85, "normal": 80, "borderline": 75},
                "blocks": [
                    {
                        "id": "B1",
                        "name": "Контакт",
                        "criteria": [
                            {"id": 1, "text": "Поздоровался", "max": 5},
                            {
                                "id": 2,
                                "text": "Заполнил CRM",
                                "max": 3,
                                "source": "crm",
                            },
                        ],
                    },
                ],
            },
        ),
    )
    dbsession.add(
        RubricVersion(
            source="op",
            version="okk_meeting_v1",
            active=True,
            definition={
                "id": "okk_meeting_v1",
                "version": "1.0",
                "max_total_score": 50,
                "criteria": [
                    {
                        "id": 1,
                        "block": "Контакт",
                        "name": "Приветствие",
                        "max_score": 5,
                        "check": "Поздоровался и представился",
                    },
                ],
            },
        ),
    )
    await dbsession.flush()

    resp = await client.get("/api/v1/rubrics", headers=head_auth)
    assert resp.status_code == 200
    tm, op = resp.json()  # tm first by contract
    assert tm["source"] == "tm"
    assert tm["version"] == "tm-call-v2"
    assert tm["max_total"] == 5.0  # crm criterion excluded
    assert [c["name"] for c in tm["criteria"]] == ["Поздоровался"]
    assert tm["criteria"][0]["block"] == "Контакт"
    assert op["source"] == "op"
    assert op["max_total"] == 50.0
    assert op["criteria"][0]["name"] == "Приветствие"
    assert op["criteria"][0]["max"] == 5.0


# --- roles: personal keys + manager/head scoping ----------------------------


@pytest.fixture
async def manager_auth(
    dbsession: AsyncSession,
    _token: None,
) -> dict[str, str]:
    """Headers for a manager session linked to Bitrix user 701."""
    await _seed_manager(dbsession, bitrix_user_id=701, name="Олжас", last_name="М.")
    await _seed_companion_user(
        dbsession,
        key=_MANAGER_KEY,
        role=CompanionRole.MANAGER,
        bitrix_user_id=701,
        name="Олжас М.",
    )
    return _headers(_MANAGER_KEY)


async def test_requires_user_key(client: AsyncClient, _token: None) -> None:
    """A valid service bearer without a personal key is rejected."""
    resp = await client.get("/api/v1/managers/1/scorecard", headers=_AUTH)
    assert resp.status_code == 401


async def test_revoked_key_rejected(
    client: AsyncClient,
    dbsession: AsyncSession,
    _token: None,
) -> None:
    """A deactivated user's key no longer authenticates."""
    user = await _seed_companion_user(
        dbsession,
        key="revoked-key",
        role=CompanionRole.HEAD,
    )
    user.active = False
    await dbsession.flush()
    resp = await client.get("/api/v1/me", headers=_headers("revoked-key"))
    assert resp.status_code == 401


async def test_me_resolves_role_and_profile(
    client: AsyncClient,
    manager_auth: dict[str, str],
    head_auth: dict[str, str],
) -> None:
    """/me returns the role + linked manager profile the cabinet boots from."""
    me = (await client.get("/api/v1/me", headers=manager_auth)).json()
    assert me["role"] == "manager"
    assert me["bitrix_user_id"] == 701
    assert me["manager"]["name"] == "Олжас М."

    me = (await client.get("/api/v1/me", headers=head_auth)).json()
    assert me["role"] == "head"
    assert me["manager"] is None


async def test_manager_sees_own_data_only(
    client: AsyncClient,
    dbsession: AsyncSession,
    manager_auth: dict[str, str],
) -> None:
    """A manager gets their own scorecard/calls but 403 on anyone else's."""
    own = await client.get(
        f"/api/v1/managers/701/scorecard?period={_PERIOD}",
        headers=manager_auth,
    )
    assert own.status_code == 200

    other = await _seed_manager(dbsession, bitrix_user_id=702)
    for path in (
        f"/api/v1/managers/{other.bitrix_user_id}/scorecard",
        f"/api/v1/managers/{other.bitrix_user_id}/calls",
        f"/api/v1/managers/{other.bitrix_user_id}/meetings",
        f"/api/v1/managers/{other.bitrix_user_id}/day",
    ):
        resp = await client.get(path, headers=manager_auth)
        assert resp.status_code == 403, path


async def test_manager_cannot_read_others_feedback(
    client: AsyncClient,
    dbsession: AsyncSession,
    manager_auth: dict[str, str],
) -> None:
    """Per-call авто-разбор is scoped: another manager's call returns 403."""
    other = await _seed_manager(dbsession, bitrix_user_id=703)
    call = await _seed_scored_call(
        dbsession,
        bitrix_call_id="other1",
        manager=other,
        percent=88.0,
    )
    resp = await client.get(f"/api/v1/calls/{call.id}/feedback", headers=manager_auth)
    assert resp.status_code == 403


async def test_meetings_scoped_to_uploader(
    client: AsyncClient,
    dbsession: AsyncSession,
    manager_auth: dict[str, str],
) -> None:
    """A manager reads their own meetings; others' (and unattributed) are 403."""
    own = await _seed_meeting(dbsession, bitrix_file_id=9101, uploaded_by=701)
    other = await _seed_meeting(dbsession, bitrix_file_id=9102, uploaded_by=703)
    orphan = await _seed_meeting(dbsession, bitrix_file_id=9103, uploaded_by=None)

    feed = await client.get("/api/v1/managers/701/meetings", headers=manager_auth)
    assert feed.status_code == 200
    assert [i["meeting_id"] for i in feed.json()] == [own.id]

    detail = await client.get(
        f"/api/v1/meetings/{own.id}/feedback",
        headers=manager_auth,
    )
    assert detail.status_code == 200

    for meeting in (other, orphan):
        resp = await client.get(
            f"/api/v1/meetings/{meeting.id}/feedback",
            headers=manager_auth,
        )
        assert resp.status_code == 403


async def test_team_summary_is_head_only(
    client: AsyncClient,
    dbsession: AsyncSession,
    manager_auth: dict[str, str],
) -> None:
    """Managers cannot pull the team-wide rollup."""
    dept = Department(bitrix_id=88, name="Отдел ТМ")
    dbsession.add(dept)
    await dbsession.flush()
    resp = await client.get("/api/v1/teams/88/summary", headers=manager_auth)
    assert resp.status_code == 403


async def test_head_sees_any_manager(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
) -> None:
    """The head of sales reads any manager's scorecard."""
    await _seed_manager(dbsession, bitrix_user_id=704)
    resp = await client.get(
        f"/api/v1/managers/704/scorecard?period={_PERIOD}",
        headers=head_auth,
    )
    assert resp.status_code == 200


# --- department-scoped head (office РОП) -------------------------------------

_DEPT_HEAD_KEY = "dept-head-personal-key"


@pytest.fixture
async def op_department(dbsession: AsyncSession) -> Department:
    """An ОП office department (Bitrix id 91) the scoped head is tied to."""
    dept = Department(bitrix_id=91, name="ОП Алматы-1")
    dbsession.add(dept)
    await dbsession.flush()
    return dept


@pytest.fixture
async def dept_head_auth(
    dbsession: AsyncSession,
    op_department: Department,
    _token: None,
) -> dict[str, str]:
    """Headers for an office РОП scoped to Bitrix department 91."""
    user = CompanionUser(
        key_sha256=hash_key(_DEPT_HEAD_KEY),
        role=CompanionRole.HEAD,
        department_id=op_department.bitrix_id,
        name="РОП Алматы-1",
    )
    dbsession.add(user)
    await dbsession.flush()
    return _headers(_DEPT_HEAD_KEY)


async def test_me_returns_department_scope(
    client: AsyncClient,
    dept_head_auth: dict[str, str],
    head_auth: dict[str, str],
) -> None:
    """/me carries the head's department scope; the global head has none."""
    me = (await client.get("/api/v1/me", headers=dept_head_auth)).json()
    assert me["role"] == "head"
    assert me["department"] == {"bitrix_id": 91, "name": "ОП Алматы-1"}

    me = (await client.get("/api/v1/me", headers=head_auth)).json()
    assert me["role"] == "head"
    assert me["department"] is None


async def test_me_manager_department_from_profile(
    client: AsyncClient,
    dbsession: AsyncSession,
    op_department: Department,
    _token: None,
) -> None:
    """A manager's /me department mirrors their managers-row department."""
    await _seed_manager(dbsession, bitrix_user_id=801, department=op_department)
    await _seed_companion_user(
        dbsession,
        key="op-manager-key",
        role=CompanionRole.MANAGER,
        bitrix_user_id=801,
    )
    me = (await client.get("/api/v1/me", headers=_headers("op-manager-key"))).json()
    assert me["department"] == {"bitrix_id": 91, "name": "ОП Алматы-1"}


async def test_dept_head_sees_only_own_department_managers(
    client: AsyncClient,
    dbsession: AsyncSession,
    op_department: Department,
    dept_head_auth: dict[str, str],
) -> None:
    """An office РОП reads their managers; other/unenriched managers are 403."""
    own = await _seed_manager(dbsession, bitrix_user_id=802, department=op_department)
    other_dept = Department(bitrix_id=92, name="ОП Астана")
    dbsession.add(other_dept)
    await dbsession.flush()
    foreign = await _seed_manager(dbsession, bitrix_user_id=803, department=other_dept)
    orphan = await _seed_manager(dbsession, bitrix_user_id=804)

    for path in (
        f"/api/v1/managers/{own.bitrix_user_id}/scorecard?period={_PERIOD}",
        f"/api/v1/managers/{own.bitrix_user_id}/meetings",
        f"/api/v1/managers/{own.bitrix_user_id}/feed",
    ):
        resp = await client.get(path, headers=dept_head_auth)
        assert resp.status_code == 200, path

    for uid in (foreign.bitrix_user_id, orphan.bitrix_user_id):
        for path in (
            f"/api/v1/managers/{uid}/scorecard",
            f"/api/v1/managers/{uid}/calls",
            f"/api/v1/managers/{uid}/meetings",
        ):
            resp = await client.get(path, headers=dept_head_auth)
            assert resp.status_code == 403, path


async def test_dept_head_meeting_feedback_scoped(
    client: AsyncClient,
    dbsession: AsyncSession,
    op_department: Department,
    dept_head_auth: dict[str, str],
) -> None:
    """Per-meeting разбор: own department 200; foreign and orphan 403."""
    own_mgr = await _seed_manager(
        dbsession,
        bitrix_user_id=805,
        department=op_department,
    )
    own = await _seed_meeting(
        dbsession,
        bitrix_file_id=9501,
        uploaded_by=805,
        manager=own_mgr,
    )
    foreign = await _seed_meeting(dbsession, bitrix_file_id=9502, uploaded_by=703)
    orphan = await _seed_meeting(dbsession, bitrix_file_id=9503, uploaded_by=None)

    resp = await client.get(
        f"/api/v1/meetings/{own.id}/feedback",
        headers=dept_head_auth,
    )
    assert resp.status_code == 200

    for meeting in (foreign, orphan):
        resp = await client.get(
            f"/api/v1/meetings/{meeting.id}/feedback",
            headers=dept_head_auth,
        )
        assert resp.status_code == 403


async def test_dept_head_team_summary_scoped(
    client: AsyncClient,
    dbsession: AsyncSession,
    dept_head_auth: dict[str, str],
) -> None:
    """The office РОП gets their own rollup; another department's is 403."""
    other_dept = Department(bitrix_id=93, name="ОП Шымкент")
    dbsession.add(other_dept)
    await dbsession.flush()

    own = await client.get("/api/v1/teams/91/summary", headers=dept_head_auth)
    assert own.status_code == 200
    assert own.json()["department"]["bitrix_id"] == 91

    foreign = await client.get("/api/v1/teams/93/summary", headers=dept_head_auth)
    assert foreign.status_code == 403


async def test_departments_list_for_global_head(
    client: AsyncClient,
    dbsession: AsyncSession,
    head_auth: dict[str, str],
) -> None:
    """The global head gets every department (Bitrix id + name), name-sorted."""
    dbsession.add_all(
        [
            Department(bitrix_id=91, name="ОП Шымкент"),
            Department(bitrix_id=92, name="ОП Астана"),
        ],
    )
    await dbsession.flush()

    resp = await client.get("/api/v1/departments", headers=head_auth)
    assert resp.status_code == 200
    assert resp.json() == [
        {"bitrix_id": 92, "name": "ОП Астана"},
        {"bitrix_id": 91, "name": "ОП Шымкент"},
    ]


async def test_departments_list_forbidden_for_scoped_head(
    client: AsyncClient,
    dept_head_auth: dict[str, str],
) -> None:
    """Listing departments stays a global-head action — an office РОП gets 403."""
    resp = await client.get("/api/v1/departments", headers=dept_head_auth)
    assert resp.status_code == 403


async def test_dept_head_lists_only_own_dept_manager_keys(
    client: AsyncClient,
    dbsession: AsyncSession,
    op_department: Department,
    dept_head_auth: dict[str, str],
    head_auth: dict[str, str],
) -> None:
    """An office РОП's /users shows only their department's manager keys."""
    await _seed_manager(dbsession, bitrix_user_id=811, department=op_department)
    other_dept = Department(bitrix_id=92, name="ОП Астана")
    dbsession.add(other_dept)
    await dbsession.flush()
    await _seed_manager(dbsession, bitrix_user_id=812, department=other_dept)

    own_key = await _seed_companion_user(
        dbsession,
        key="k-own",
        role=CompanionRole.MANAGER,
        bitrix_user_id=811,
    )
    await _seed_companion_user(
        dbsession,
        key="k-foreign",
        role=CompanionRole.MANAGER,
        bitrix_user_id=812,
    )
    # No managers row at all — stays global-head-only.
    await _seed_companion_user(
        dbsession,
        key="k-orphan",
        role=CompanionRole.MANAGER,
        bitrix_user_id=813,
    )

    scoped = (await client.get("/api/v1/users", headers=dept_head_auth)).json()
    assert [u["id"] for u in scoped] == [own_key.id]

    global_list = (await client.get("/api/v1/users", headers=head_auth)).json()
    assert len(global_list) >= 4  # all three manager keys + head rows


async def test_dept_head_issues_key_tied_to_department(
    client: AsyncClient,
    dbsession: AsyncSession,
    op_department: Department,
    dept_head_auth: dict[str, str],
) -> None:
    """A scoped head's new manager key ties the manager to their department."""
    resp = await client.post(
        "/api/v1/users",
        json={"bitrix_user_id": 821, "name": "Новый Менеджер"},
        headers=dept_head_auth,
    )
    assert resp.status_code == 201
    created = resp.json()
    assert created["user"]["role"] == "manager"

    mgr = await dbsession.scalar(
        select(Manager).where(Manager.bitrix_user_id == 821),
    )
    assert mgr is not None
    assert mgr.department_id == op_department.id
    assert mgr.enriched is True  # ingestion must not re-derive the department

    me = (await client.get("/api/v1/me", headers=_headers(created["key"]))).json()
    assert me["role"] == "manager"
    assert me["department"] == {"bitrix_id": 91, "name": "ОП Алматы-1"}


async def test_dept_head_issue_overrides_existing_department(
    client: AsyncClient,
    dbsession: AsyncSession,
    op_department: Department,
    dept_head_auth: dict[str, str],
) -> None:
    """Cabinet wins: issuing a key moves the manager into the head's dept."""
    other_dept = Department(bitrix_id=94, name="ОП Караганда")
    dbsession.add(other_dept)
    await dbsession.flush()
    mgr = await _seed_manager(dbsession, bitrix_user_id=822, department=other_dept)

    resp = await client.post(
        "/api/v1/users",
        json={"bitrix_user_id": 822},
        headers=dept_head_auth,
    )
    assert resp.status_code == 201
    await dbsession.refresh(mgr)
    assert mgr.department_id == op_department.id
    assert mgr.enriched is True


async def test_dept_head_cannot_mint_head(
    client: AsyncClient,
    dept_head_auth: dict[str, str],
) -> None:
    """Minting head keys stays with the global head — scoped heads get 403."""
    resp = await client.post(
        "/api/v1/users",
        json={"role": "head", "department_id": 91, "name": "Самозванец"},
        headers=dept_head_auth,
    )
    assert resp.status_code == 403


async def test_manager_key_payload_rejects_department_id(
    client: AsyncClient,
    dept_head_auth: dict[str, str],
) -> None:
    """A manager key's department comes from the issuer's scope, not payload."""
    resp = await client.post(
        "/api/v1/users",
        json={"bitrix_user_id": 9, "name": "x", "department_id": 91},
        headers=dept_head_auth,
    )
    assert resp.status_code == 422


async def test_dept_head_revokes_only_own_dept_keys(
    client: AsyncClient,
    dbsession: AsyncSession,
    op_department: Department,
    dept_head_auth: dict[str, str],
) -> None:
    """Revocation by a scoped head: own dept 200; foreign/orphan/head 403."""
    await _seed_manager(dbsession, bitrix_user_id=831, department=op_department)
    other_dept = Department(bitrix_id=95, name="ОП Актобе")
    dbsession.add(other_dept)
    await dbsession.flush()
    await _seed_manager(dbsession, bitrix_user_id=832, department=other_dept)

    own = await _seed_companion_user(
        dbsession,
        key="r-own",
        role=CompanionRole.MANAGER,
        bitrix_user_id=831,
    )
    foreign = await _seed_companion_user(
        dbsession,
        key="r-foreign",
        role=CompanionRole.MANAGER,
        bitrix_user_id=832,
    )
    orphan = await _seed_companion_user(
        dbsession,
        key="r-orphan",
        role=CompanionRole.MANAGER,
        bitrix_user_id=833,
    )
    own_head_row = await dbsession.scalar(
        select(CompanionUser).where(
            CompanionUser.key_sha256 == hash_key(_DEPT_HEAD_KEY),
        ),
    )
    assert own_head_row is not None

    resp = await client.post(
        f"/api/v1/users/{own.id}/revoke",
        headers=dept_head_auth,
    )
    assert resp.status_code == 200
    assert (
        await client.get("/api/v1/me", headers=_headers("r-own"))
    ).status_code == 401

    for row in (foreign, orphan, own_head_row):
        resp = await client.post(
            f"/api/v1/users/{row.id}/revoke",
            headers=dept_head_auth,
        )
        assert resp.status_code == 403, row.id


async def test_global_head_unaffected_by_scoping(
    client: AsyncClient,
    dbsession: AsyncSession,
    op_department: Department,
    head_auth: dict[str, str],
) -> None:
    """The global head still reads any department's data."""
    await _seed_manager(dbsession, bitrix_user_id=806, department=op_department)
    resp = await client.get("/api/v1/managers/806/scorecard", headers=head_auth)
    assert resp.status_code == 200
    resp = await client.get("/api/v1/teams/91/summary", headers=head_auth)
    assert resp.status_code == 200


# --- static РОП key + cabinet access management ------------------------------

_STATIC_HEAD_KEY = "static-rop-code"


@pytest.fixture
def static_head_auth(
    monkeypatch: pytest.MonkeyPatch,
    _token: None,
) -> dict[str, str]:
    """Headers for the РОП logged in with the static configured key (no DB row)."""
    monkeypatch.setattr(settings, "companion_head_key", _STATIC_HEAD_KEY)
    return _headers(_STATIC_HEAD_KEY)


async def test_static_head_key_grants_head(
    client: AsyncClient,
    static_head_auth: dict[str, str],
) -> None:
    """The configured static key logs in as HEAD without a companion_users row."""
    me = (await client.get("/api/v1/me", headers=static_head_auth)).json()
    assert me["role"] == "head"

    resp = await client.get("/api/v1/users", headers=static_head_auth)
    assert resp.status_code == 200
    assert resp.json() == []


async def test_static_head_key_inert_when_unset(
    client: AsyncClient,
    _token: None,
) -> None:
    """With companion_head_key unset, the would-be static code is a 401."""
    resp = await client.get("/api/v1/me", headers=_headers(_STATIC_HEAD_KEY))
    assert resp.status_code == 401


async def test_head_issues_manager_key(
    client: AsyncClient,
    dbsession: AsyncSession,
    static_head_auth: dict[str, str],
) -> None:
    """POST /users issues a working manager key, scoped to its Bitrix user."""
    await _seed_manager(dbsession, bitrix_user_id=705, name="Айгуль", last_name="С.")
    resp = await client.post(
        "/api/v1/users",
        json={"bitrix_user_id": 705, "name": "Айгуль С."},
        headers=static_head_auth,
    )
    assert resp.status_code == 201
    created = resp.json()
    assert created["user"]["role"] == "manager"
    assert created["user"]["active"] is True

    mgr_headers = _headers(created["key"])
    me = (await client.get("/api/v1/me", headers=mgr_headers)).json()
    assert me["role"] == "manager"
    assert me["bitrix_user_id"] == 705

    own = await client.get("/api/v1/managers/705/scorecard", headers=mgr_headers)
    assert own.status_code == 200
    other = await client.get("/api/v1/managers/999/scorecard", headers=mgr_headers)
    assert other.status_code == 403


async def test_issue_key_name_resolved_from_managers_table(
    client: AsyncClient,
    dbsession: AsyncSession,
    static_head_auth: dict[str, str],
) -> None:
    """Omitting 'name' pulls it from Bitrix data (OKK's managers table)."""
    await _seed_manager(dbsession, bitrix_user_id=707, name="Динара", last_name="К.")
    resp = await client.post(
        "/api/v1/users",
        json={"bitrix_user_id": 707},
        headers=static_head_auth,
    )
    assert resp.status_code == 201
    assert resp.json()["user"]["name"] == "Динара К."


async def test_issue_key_name_resolved_via_live_bitrix(
    client: AsyncClient,
    static_head_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manager the pipeline hasn't seen yet is resolved by live user.get."""

    async def _fake_bitrix(_uid: int) -> str:
        return "Айдар Н."

    monkeypatch.setattr(service, "_bitrix_user_name", _fake_bitrix)
    resp = await client.post(
        "/api/v1/users",
        json={"bitrix_user_id": 708},
        headers=static_head_auth,
    )
    assert resp.status_code == 201
    assert resp.json()["user"]["name"] == "Айдар Н."


async def test_issue_key_unresolvable_name_is_422(
    client: AsyncClient,
    static_head_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No managers row and Bitrix can't resolve the id → explicit 422."""

    async def _no_bitrix(_uid: int) -> None:
        return None

    monkeypatch.setattr(service, "_bitrix_user_name", _no_bitrix)
    resp = await client.post(
        "/api/v1/users",
        json={"bitrix_user_id": 999999},
        headers=static_head_auth,
    )
    assert resp.status_code == 422


async def test_users_endpoints_are_head_only(
    client: AsyncClient,
    manager_auth: dict[str, str],
) -> None:
    """Managers cannot list, issue, or revoke cabinet keys."""
    assert (await client.get("/api/v1/users", headers=manager_auth)).status_code == 403
    assert (
        await client.post(
            "/api/v1/users",
            json={"bitrix_user_id": 9, "name": "x"},
            headers=manager_auth,
        )
    ).status_code == 403
    assert (
        await client.post("/api/v1/users/1/revoke", headers=manager_auth)
    ).status_code == 403


async def test_revoked_manager_key_stops_working(
    client: AsyncClient,
    static_head_auth: dict[str, str],
) -> None:
    """Revoking from the cabinet deactivates the key immediately."""
    created = (
        await client.post(
            "/api/v1/users",
            json={"bitrix_user_id": 706, "name": "Темп"},
            headers=static_head_auth,
        )
    ).json()
    mgr_headers = _headers(created["key"])
    assert (await client.get("/api/v1/me", headers=mgr_headers)).status_code == 200

    revoked = await client.post(
        f"/api/v1/users/{created['user']['id']}/revoke",
        headers=static_head_auth,
    )
    assert revoked.status_code == 200
    assert revoked.json()["active"] is False
    assert (await client.get("/api/v1/me", headers=mgr_headers)).status_code == 401


async def test_global_head_rows_not_revocable_from_cabinet(
    client: AsyncClient,
    dbsession: AsyncSession,
    static_head_auth: dict[str, str],
) -> None:
    """Dept-NULL (global) head rows stay env/CLI-managed — cabinet 403."""
    head_row = await _seed_companion_user(
        dbsession,
        key="db-head-key",
        role=CompanionRole.HEAD,
        name="Второй РОП",
    )
    resp = await client.post(
        f"/api/v1/users/{head_row.id}/revoke",
        headers=static_head_auth,
    )
    assert resp.status_code == 403


async def test_global_head_mints_scoped_head_key(
    client: AsyncClient,
    op_department: Department,
    static_head_auth: dict[str, str],
) -> None:
    """POST /users with role=head issues a working department-scoped РОП key."""
    resp = await client.post(
        "/api/v1/users",
        json={"role": "head", "department_id": 91, "name": "РОП Алматы-1"},
        headers=static_head_auth,
    )
    assert resp.status_code == 201
    created = resp.json()
    assert created["user"]["role"] == "head"
    assert created["user"]["department_id"] == 91

    rop_headers = _headers(created["key"])
    me = (await client.get("/api/v1/me", headers=rop_headers)).json()
    assert me["role"] == "head"
    assert me["department"] == {"bitrix_id": 91, "name": "ОП Алматы-1"}

    assert (
        await client.get("/api/v1/teams/91/summary", headers=rop_headers)
    ).status_code == 200
    assert (
        await client.get("/api/v1/teams/92/summary", headers=rop_headers)
    ).status_code == 403


async def test_cabinet_never_mints_global_head(
    client: AsyncClient,
    static_head_auth: dict[str, str],
) -> None:
    """role=head needs a department_id and a name source — both are 422."""
    resp = await client.post(
        "/api/v1/users",
        json={"role": "head", "name": "Глобальный"},
        headers=static_head_auth,
    )
    assert resp.status_code == 422

    resp = await client.post(
        "/api/v1/users",
        json={"role": "head", "department_id": 91},
        headers=static_head_auth,
    )
    assert resp.status_code == 422


async def test_global_head_revokes_scoped_head_key(
    client: AsyncClient,
    dbsession: AsyncSession,
    static_head_auth: dict[str, str],
) -> None:
    """Scoped-head keys are cabinet-revocable by the global head."""
    row = await _seed_companion_user(
        dbsession,
        key="scoped-rop-key",
        role=CompanionRole.HEAD,
        department_id=96,
        name="РОП офиса",
    )
    assert (
        await client.get("/api/v1/me", headers=_headers("scoped-rop-key"))
    ).status_code == 200

    resp = await client.post(
        f"/api/v1/users/{row.id}/revoke",
        headers=static_head_auth,
    )
    assert resp.status_code == 200
    assert (
        await client.get("/api/v1/me", headers=_headers("scoped-rop-key"))
    ).status_code == 401


async def test_global_head_issue_keeps_no_dept_tie(
    client: AsyncClient,
    dbsession: AsyncSession,
    static_head_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The global head's manager keys don't touch the managers table."""

    async def _fake_bitrix(_uid: int) -> str:
        return "Без Отдела"

    monkeypatch.setattr(service, "_bitrix_user_name", _fake_bitrix)
    resp = await client.post(
        "/api/v1/users",
        json={"bitrix_user_id": 841},
        headers=static_head_auth,
    )
    assert resp.status_code == 201
    mgr = await dbsession.scalar(
        select(Manager).where(Manager.bitrix_user_id == 841),
    )
    assert mgr is None


# --- appeals (апелляции) -----------------------------------------------------


async def _manager_701(session: AsyncSession) -> Manager:
    """The manager the ``manager_auth`` fixture already seeded (Bitrix 701)."""
    mgr = await session.scalar(select(Manager).where(Manager.bitrix_user_id == 701))
    assert mgr is not None
    return mgr


async def _seed_call_with_criteria(
    session: AsyncSession,
    *,
    bitrix_call_id: str,
    manager: Manager,
    scores: list[tuple[int, int, int]],
    flags: list[str] | None = None,
) -> Call:
    """Seed a SCORED call whose per_criterion is ``[(id, score, max), ...]``.

    ``total_score`` is the percent those criteria imply, so the headline matches
    the breakdown (and an appeal recompute is meaningful).
    """
    raw = sum(s for _, s, _ in scores)
    mx = sum(m for _, _, m in scores)
    percent = round(100.0 * raw / mx, 2) if mx else 0.0
    call = Call(
        bitrix_call_id=bitrix_call_id,
        portal_user_id=manager.bitrix_user_id,
        manager_id=manager.id,
        direction=CallDirection.OUTBOUND,
        started_at=datetime(2026, 3, 15, 12, 0, tzinfo=_TZ),
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
            criteria={
                "per_criterion": [
                    {
                        "id": cid,
                        "block_id": f"B{cid}",
                        "block_name": f"Блок {cid}",
                        "text": f"Критерий {cid}",
                        "score": s,
                        "max": m,
                        "justification": "ок",
                        "evidence": "—",
                    }
                    for cid, s, m in scores
                ],
                "blocks": {},
                "raw_points": raw,
                "max_points": mx,
                "percent": percent,
                "zone": "strong" if percent >= 85 else "risk",
                "call_type": "квалификация",
                "is_qualification_call": True,
                "manager_identified": True,
                "objections_present": True,
                "target_status": "целевой",
                "strengths": "—",
                "growth_zone": "—",
                "training_recommendation": "—",
            },
            sentiment={"customer": "позитивный", "agent": "нейтральный"},
            summary="Тестовый звонок.",
            flags=flags or [],
            model="fake/test",
        ),
    )
    await session.flush()
    return call


async def test_manager_files_appeal(
    client: AsyncClient,
    dbsession: AsyncSession,
    manager_auth: dict[str, str],
) -> None:
    """A manager files a per-criterion appeal → pending, with enriched criteria."""
    mgr = await _manager_701(dbsession)
    call = await _seed_scored_call(
        dbsession,
        bitrix_call_id="ap1",
        manager=mgr,
        percent=70.0,
    )
    resp = await client.post(
        f"/api/v1/calls/{call.id}/appeal",
        headers=manager_auth,
        json={
            "disputed_criteria": [
                {"criterion_id": 1, "reason": "Я поздоровался, СТТ не распознал"},
            ],
            "reason": "Оценка несправедлива",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert body["call_id"] == call.id
    assert body["manager_bitrix_user_id"] == 701
    assert body["original_percent"] == 70.0
    assert body["override_percent"] is None
    assert body["confirmed_criteria"] == []
    # The contested criterion is enriched with its scored text + max for the РОП.
    disputed = body["disputed_criteria"]
    assert len(disputed) == 1
    assert disputed[0]["criterion_id"] == 1
    assert disputed[0]["criterion_text"] == "Поздоровался"
    assert disputed[0]["original_score"] == 4.0
    assert disputed[0]["max"] == 5.0
    assert disputed[0]["confirmed"] is False
    assert disputed[0]["reason"].startswith("Я поздоровался")


async def test_appeal_unknown_criterion_422(
    client: AsyncClient,
    dbsession: AsyncSession,
    manager_auth: dict[str, str],
) -> None:
    """Disputing a criterion that isn't on the call is rejected."""
    mgr = await _manager_701(dbsession)
    call = await _seed_scored_call(
        dbsession,
        bitrix_call_id="ap-unknown",
        manager=mgr,
        percent=70.0,
    )
    resp = await client.post(
        f"/api/v1/calls/{call.id}/appeal",
        headers=manager_auth,
        json={"disputed_criteria": [{"criterion_id": 99}]},
    )
    assert resp.status_code == 422


async def test_manager_cannot_appeal_another_managers_call(
    client: AsyncClient,
    dbsession: AsyncSession,
    manager_auth: dict[str, str],
) -> None:
    """Only the call's own manager may appeal it."""
    other = await _seed_manager(dbsession, bitrix_user_id=702)
    call = await _seed_scored_call(
        dbsession,
        bitrix_call_id="ap-other",
        manager=other,
        percent=60.0,
    )
    resp = await client.post(
        f"/api/v1/calls/{call.id}/appeal",
        headers=manager_auth,
        json={"reason": "не моя"},
    )
    assert resp.status_code == 403


async def test_appeal_on_unscored_call_404(
    client: AsyncClient,
    manager_auth: dict[str, str],
) -> None:
    """Appealing a call with no score returns 404."""
    resp = await client.post(
        "/api/v1/calls/999999/appeal",
        headers=manager_auth,
        json={"reason": "нет такой"},
    )
    assert resp.status_code == 404


async def test_duplicate_pending_appeal_conflicts(
    client: AsyncClient,
    dbsession: AsyncSession,
    manager_auth: dict[str, str],
) -> None:
    """A second appeal while one is still pending returns 409."""
    mgr = await _manager_701(dbsession)
    call = await _seed_scored_call(
        dbsession,
        bitrix_call_id="ap-dup",
        manager=mgr,
        percent=72.0,
    )
    path = f"/api/v1/calls/{call.id}/appeal"
    first = await client.post(path, headers=manager_auth, json={"reason": "раз"})
    assert first.status_code == 201
    second = await client.post(path, headers=manager_auth, json={"reason": "два"})
    assert second.status_code == 409


async def test_head_confirms_criterion_and_score_recalculates(
    client: AsyncClient,
    dbsession: AsyncSession,
    manager_auth: dict[str, str],
    head_auth: dict[str, str],
) -> None:
    """Head confirms one of two contested criteria; the total recomputes itself."""
    mgr = await _manager_701(dbsession)
    # Two criteria, 2/5 and 3/5 → 5/10 = 50%.
    call = await _seed_call_with_criteria(
        dbsession,
        bitrix_call_id="ap-recalc",
        manager=mgr,
        scores=[(1, 2, 5), (2, 3, 5)],
    )
    filed = await client.post(
        f"/api/v1/calls/{call.id}/appeal",
        headers=manager_auth,
        json={
            "disputed_criteria": [
                {"criterion_id": 1, "reason": "поздоровался"},
                {"criterion_id": 2, "reason": "выявил потребность"},
            ],
            "reason": "СТТ не распознал часть разговора",
        },
    )
    appeal_id = filed.json()["id"]

    # The head's review queue surfaces it with the contested criteria enriched.
    queue = await client.get("/api/v1/appeals?status=pending", headers=head_auth)
    assert queue.status_code == 200
    rows = queue.json()
    assert [r["id"] for r in rows] == [appeal_id]
    assert rows[0]["original_percent"] == 50.0
    assert {c["criterion_id"] for c in rows[0]["disputed_criteria"]} == {1, 2}

    # Confirm only criterion 1 → it gets full marks (5), criterion 2 stays 3:
    # (5 + 3) / 10 = 80%.
    review = await client.post(
        f"/api/v1/appeals/{appeal_id}/review",
        headers=head_auth,
        json={"confirmed_criteria": [1], "note": "прослушал, менеджер прав по п.1"},
    )
    assert review.status_code == 200
    rb = review.json()
    assert rb["status"] == "accepted"
    assert rb["confirmed_criteria"] == [1]
    assert rb["override_percent"] == 80.0
    assert rb["override_okk_5"] == 3

    # The recomputed score now drives the per-call feedback...
    fb = (
        await client.get(f"/api/v1/calls/{call.id}/feedback", headers=head_auth)
    ).json()
    assert fb["percent"] == 80.0
    assert fb["okk_5"] == 3
    assert fb["appeal"]["status"] == "accepted"
    assert fb["appeal"]["original_percent"] == 50.0
    # ...and the confirmed criterion shows at full marks, the other unchanged.
    by_id = {c["criterion_id"]: c for c in fb["criteria"]}
    assert by_id[1]["corrected"] is True
    assert by_id[1]["score"] == 5.0
    assert by_id[1]["percent_of_max"] == 100.0
    assert by_id[2]["corrected"] is False
    assert by_id[2]["score"] == 3.0

    # ...and the aggregate scorecard.
    card = (
        await client.get(
            f"/api/v1/managers/701/scorecard?period={_PERIOD}",
            headers=head_auth,
        )
    ).json()
    assert card["okk"]["percent"] == 80.0


async def test_head_dismisses_red_flag_when_accepting_appeal(
    client: AsyncClient,
    dbsession: AsyncSession,
    manager_auth: dict[str, str],
    head_auth: dict[str, str],
) -> None:
    """Accepting an appeal clears a related red flag the head ticks as resolved."""
    mgr = await _manager_701(dbsession)
    call = await _seed_call_with_criteria(
        dbsession,
        bitrix_call_id="ap-flag",
        manager=mgr,
        scores=[(1, 2, 5), (2, 4, 5)],
        flags=["Не провёл презентацию ЖК", "Обещал скидку без согласования"],
    )

    filed = await client.post(
        f"/api/v1/calls/{call.id}/appeal",
        headers=manager_auth,
        json={
            "disputed_criteria": [{"criterion_id": 1, "reason": "презентацию провёл"}],
            "reason": "СТТ не распознал презентацию",
        },
    )
    appeal_id = filed.json()["id"]

    # The head's queue surfaces the call's red flags so they can be cleared.
    resp = await client.get("/api/v1/appeals?status=pending", headers=head_auth)
    queue = resp.json()
    assert set(queue[0]["red_flags"]) == {
        "Не провёл презентацию ЖК",
        "Обещал скидку без согласования",
    }

    # Confirm criterion 1 and clear the presentation flag — leave the other flag.
    review = await client.post(
        f"/api/v1/appeals/{appeal_id}/review",
        headers=head_auth,
        json={
            "confirmed_criteria": [1],
            "dismissed_flags": ["Не провёл презентацию ЖК"],
            "note": "презентация была, флаг снимаю",
        },
    )
    assert review.status_code == 200
    assert review.json()["dismissed_flags"] == ["Не провёл презентацию ЖК"]

    # The cleared flag is gone from the call detail; the unrelated one remains.
    fb = (
        await client.get(f"/api/v1/calls/{call.id}/feedback", headers=head_auth)
    ).json()
    assert fb["red_flags"] == ["Обещал скидку без согласования"]

    # ...and from the manager's feed item for the same call.
    feed = (
        await client.get("/api/v1/managers/701/calls", headers=head_auth)
    ).json()
    item = next(c for c in feed if c["call_id"] == call.id)
    assert item["red_flags"] == ["Обещал скидку без согласования"]


async def test_rejected_appeal_keeps_red_flags(
    client: AsyncClient,
    dbsession: AsyncSession,
    manager_auth: dict[str, str],
    head_auth: dict[str, str],
) -> None:
    """Confirming no criteria rejects the appeal; ticked flags are not cleared."""
    mgr = await _manager_701(dbsession)
    call = await _seed_call_with_criteria(
        dbsession,
        bitrix_call_id="ap-flag-reject",
        manager=mgr,
        scores=[(1, 2, 5)],
        flags=["Не провёл презентацию ЖК"],
    )
    filed = await client.post(
        f"/api/v1/calls/{call.id}/appeal",
        headers=manager_auth,
        json={"disputed_criteria": [{"criterion_id": 1, "reason": "—"}]},
    )
    appeal_id = filed.json()["id"]

    review = await client.post(
        f"/api/v1/appeals/{appeal_id}/review",
        headers=head_auth,
        json={
            "confirmed_criteria": [],
            "dismissed_flags": ["Не провёл презентацию ЖК"],
        },
    )
    assert review.json()["status"] == "rejected"

    fb = (
        await client.get(f"/api/v1/calls/{call.id}/feedback", headers=head_auth)
    ).json()
    assert fb["red_flags"] == ["Не провёл презентацию ЖК"]


async def test_review_confirm_outside_disputed_422(
    client: AsyncClient,
    dbsession: AsyncSession,
    manager_auth: dict[str, str],
    head_auth: dict[str, str],
) -> None:
    """A head can't confirm a criterion the manager never contested."""
    mgr = await _manager_701(dbsession)
    call = await _seed_call_with_criteria(
        dbsession,
        bitrix_call_id="ap-outside",
        manager=mgr,
        scores=[(1, 2, 5), (2, 3, 5)],
    )
    filed = await client.post(
        f"/api/v1/calls/{call.id}/appeal",
        headers=manager_auth,
        json={"disputed_criteria": [{"criterion_id": 1}]},
    )
    appeal_id = filed.json()["id"]
    resp = await client.post(
        f"/api/v1/appeals/{appeal_id}/review",
        headers=head_auth,
        json={"confirmed_criteria": [2]},
    )
    assert resp.status_code == 422


async def test_head_rejects_appeal_leaves_score(
    client: AsyncClient,
    dbsession: AsyncSession,
    manager_auth: dict[str, str],
    head_auth: dict[str, str],
) -> None:
    """Confirming nothing rejects the appeal and leaves the original score."""
    mgr = await _manager_701(dbsession)
    call = await _seed_scored_call(
        dbsession,
        bitrix_call_id="ap-rej",
        manager=mgr,
        percent=70.0,
    )
    filed = await client.post(
        f"/api/v1/calls/{call.id}/appeal",
        headers=manager_auth,
        json={"disputed_criteria": [{"criterion_id": 1}], "reason": "не согласен"},
    )
    appeal_id = filed.json()["id"]
    review = await client.post(
        f"/api/v1/appeals/{appeal_id}/review",
        headers=head_auth,
        json={"confirmed_criteria": [], "note": "оценка верная"},
    )
    assert review.status_code == 200
    rb = review.json()
    assert rb["status"] == "rejected"
    assert rb["override_percent"] is None

    fb = (
        await client.get(f"/api/v1/calls/{call.id}/feedback", headers=head_auth)
    ).json()
    assert fb["percent"] == 70.0
    assert fb["appeal"]["status"] == "rejected"
    assert fb["criteria"][0]["corrected"] is False


async def test_manager_cannot_review_appeals(
    client: AsyncClient,
    dbsession: AsyncSession,
    manager_auth: dict[str, str],
) -> None:
    """The review endpoint is head-only."""
    mgr = await _manager_701(dbsession)
    call = await _seed_scored_call(
        dbsession,
        bitrix_call_id="ap-mgr-review",
        manager=mgr,
        percent=70.0,
    )
    filed = await client.post(
        f"/api/v1/calls/{call.id}/appeal",
        headers=manager_auth,
        json={"disputed_criteria": [{"criterion_id": 1}]},
    )
    appeal_id = filed.json()["id"]
    resp = await client.post(
        f"/api/v1/appeals/{appeal_id}/review",
        headers=manager_auth,
        json={"confirmed_criteria": [1]},
    )
    assert resp.status_code == 403


async def test_scoped_head_only_reviews_own_department(
    client: AsyncClient,
    dbsession: AsyncSession,
    op_department: Department,
    head_auth: dict[str, str],
    dept_head_auth: dict[str, str],
) -> None:
    """An office head can't review an appeal from outside their department."""
    # A manager in a *different* department files an appeal.
    other_dept = Department(bitrix_id=92, name="ОП Астана")
    dbsession.add(other_dept)
    await dbsession.flush()
    mgr = await _seed_manager(
        dbsession,
        bitrix_user_id=910,
        department=other_dept,
    )
    await _seed_companion_user(
        dbsession,
        key="mgr-910-key",
        role=CompanionRole.MANAGER,
        bitrix_user_id=910,
    )
    call = await _seed_scored_call(
        dbsession,
        bitrix_call_id="ap-foreign",
        manager=mgr,
        percent=70.0,
    )
    filed = await client.post(
        f"/api/v1/calls/{call.id}/appeal",
        headers=_headers("mgr-910-key"),
        json={
            "disputed_criteria": [{"criterion_id": 1}],
            "reason": "из другого отдела",
        },
    )
    appeal_id = filed.json()["id"]

    # The dept-91 head is scoped out of it…
    blocked = await client.post(
        f"/api/v1/appeals/{appeal_id}/review",
        headers=dept_head_auth,
        json={"confirmed_criteria": []},
    )
    assert blocked.status_code == 403
    # …but the global head can review it.
    ok = await client.post(
        f"/api/v1/appeals/{appeal_id}/review",
        headers=head_auth,
        json={"confirmed_criteria": []},
    )
    assert ok.status_code == 200
