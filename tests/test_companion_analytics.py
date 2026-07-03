"""Companion /analytics — funnel/CR, tasks, meetings and calls blocks.

The analytics view reads straight through to Bitrix (stage history, activities,
telephony), reusing ``day``'s cache-backed helpers. These tests fake the Bitrix
client and verify each block's counting, the re-booking («переназначились») split,
the deadline-clamped task buckets, the telephony breakdown, and the endpoint's
role scoping (a manager can only see their own analytics).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from AtamuraOKK.db.models.companion_user import CompanionUser
from AtamuraOKK.db.models.enums import CompanionRole
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.settings import settings
from AtamuraOKK.web.api.v1 import analytics, day
from AtamuraOKK.web.api.v1.auth import hash_key

pytestmark = pytest.mark.anyio

_TZ = ZoneInfo(settings.report_timezone)
_FIELD = settings.companion_tm_employee_field
_WON = settings.companion_meeting_stage_id
_SOLD = settings.companion_sold_stage_id
_QUALIFIED = settings.companion_qualified_stage_id
_MEETING_SET = settings.companion_meeting_set_stage_id
_NO_SHOW = settings.companion_no_show_stage_id
_NO_ANSWER = settings.companion_no_answer_stage_ids
_PAGE = 50

# A window safely in the past (now > end) so the task helper routes incomplete
# activities to «просрочено» and skips the «открыто» read deterministically.
_PAST_START = datetime(2020, 1, 1, tzinfo=_TZ)
_PAST_END = datetime(2020, 2, 1, tzinfo=_TZ)
# A window safely in the future (now < start) → incomplete activities are «открыто».
_FUT_START = datetime(2099, 1, 1, tzinfo=_TZ)
_FUT_END = datetime(2099, 2, 1, tzinfo=_TZ)


@pytest.fixture(autouse=True)
def _fresh_caches() -> None:
    analytics._cache.clear()
    analytics._trend_cache.clear()
    analytics._enum_label_cache.clear()
    day._cache.clear()
    day._meetings_cache.clear()
    day._sold_cache.clear()
    day._entrants_cache.clear()
    day._outcomes_cache.clear()
    day._won_month_cache.clear()


class FakeBitrix:
    """Replays the reads the analytics blocks make.

    ``stage_owners`` maps a stage STATUS_ID to the list of OWNER_IDs (deal ids)
    that entered it in the window — repeats encode re-entries. ``assignee`` and
    ``tm`` map a deal id to its ASSIGNED_BY_ID / «Сотрудник ТМ» field. ``leads``
    is the envelope total returned for the deal-creation count; ``closed`` /
    ``not_done`` for the two crm.activity.list count reads; ``calls`` are the
    telephony rows.
    """

    def __init__(
        self,
        *,
        stage_owners: dict[str, list[int]] | None = None,
        assignee: dict[int, Any] | None = None,
        tm: dict[int, Any] | None = None,
        leads: int = 0,
        closed: int = 0,
        not_done: int = 0,
        closed_lost: int = 0,
        closed_lost_reasons: dict[int, Any] | None = None,
        reason_items: dict[int, str] | None = None,
        calls: list[dict[str, Any]] | None = None,
    ) -> None:
        self.stage_owners = stage_owners or {}
        self.assignee = assignee or {}
        self.tm = tm or {}
        self.leads = leads
        self.closed = closed
        self.not_done = not_done
        self.closed_lost = closed_lost
        # {deal id: отказ-причина enum id (or None)} — the lost deals the reason
        # split pages; ``reason_items`` maps enum id -> label (crm.deal.fields).
        self.closed_lost_reasons = closed_lost_reasons or {}
        self.reason_items = reason_items or {}
        self._calls = calls or []
        self.seen: list[str] = []

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Field metadata for the отказ-причина enum (crm.deal.fields)."""
        self.seen.append(method)
        if method == "crm.deal.fields":
            field = settings.companion_closed_reason_field
            items = [{"ID": str(i), "VALUE": v} for i, v in self.reason_items.items()]
            return {field: {"type": "enumeration", "items": items}}
        raise AssertionError(f"unexpected call {method}")

    async def call_raw(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Paged stage-history envelopes and the count envelopes (deals/activities)."""
        self.seen.append(method)
        params = params or {}
        flt = params.get("filter") or {}
        if method == "crm.stagehistory.list":
            stage = flt.get("STAGE_ID")
            # STAGE_ID may be one stage or a set (e.g. the two Недозвон stages) —
            # union the owners across every stage the filter names.
            stages = stage if isinstance(stage, list) else [stage]
            owners = [o for st in stages for o in self.stage_owners.get(str(st), [])]
            start = int(params.get("start") or 0)
            chunk = owners[start : start + _PAGE]
            items = [{"OWNER_ID": i} for i in chunk]
            env: dict[str, Any] = {"result": {"items": items}}
            if start + _PAGE < len(owners):
                env["next"] = start + _PAGE
            return env
        if method == "crm.deal.list":
            # Two distinct count reads share this method: the leads count (by
            # DATE_CREATE) and the closed-lost count (by STAGE_SEMANTIC_ID='F').
            if flt.get("STAGE_SEMANTIC_ID") == "F":
                return {"result": [], "total": self.closed_lost}
            return {"result": [], "total": self.leads}
        if method == "crm.activity.list":
            total = self.closed if flt.get("COMPLETED") == "Y" else self.not_done
            return {"result": [], "total": total}
        raise AssertionError(f"unexpected call_raw {method}")

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Telephony rows and deal id-lookup rows (assignee + «Сотрудник ТМ»)."""
        self.seen.append(method)
        if method == "voximplant.statistic.get":
            for row in self._calls:
                yield row
            return
        if method == "crm.deal.list":
            flt = (params or {}).get("filter") or {}
            if flt.get("STAGE_SEMANTIC_ID") == "F":  # closed-lost reason paging
                field = settings.companion_closed_reason_field
                for deal_id, reason in self.closed_lost_reasons.items():
                    yield {"ID": str(deal_id), field: reason}
                return
            for deal_id in flt["ID"]:  # id lookup carries both attributions
                yield {
                    "ID": str(deal_id),
                    "ASSIGNED_BY_ID": self.assignee.get(int(deal_id)),
                    _FIELD: self.tm.get(int(deal_id)),
                }
            return
        raise AssertionError(f"unexpected list {method}")


# --- stage_outcomes_by_assignee (the shared funnel/meetings helper) ----------


async def test_stage_outcomes_distinct_and_rebooked() -> None:
    """Distinct entrants and 2+-entry re-bookings, both attributed by assignee."""
    bx = FakeBitrix(
        # deal 10 entered twice (re-booking), 11 once, 12 once
        stage_owners={_MEETING_SET: [10, 10, 11, 12]},
        assignee={10: "5", 11: 5, 12: "9"},
    )
    distinct, rebooked = await day.stage_outcomes_by_assignee(
        bx,  # type: ignore[arg-type]
        _MEETING_SET,
        _PAST_START,
        _PAST_END,
    )
    assert distinct == {5: 2, 9: 1}  # 10 & 11 for uid 5, 12 for uid 9
    assert rebooked == {5: 1}  # only deal 10 re-entered


async def test_sold_deals_attributed_via_tm_field() -> None:
    """«Купили» = cat-2 C2:WON transitions, attributed via «Сотрудник ТМ», deduped."""
    bx = FakeBitrix(
        # deal 21 booked twice (still one sale), 22 once; 23 has no TM
        stage_owners={_SOLD: [21, 21, 22, 23]},
        assignee={21: 5, 22: 5, 23: 5},  # assignee is the closer, NOT used here
        tm={21: "5", 22: "8", 23: "0"},  # attribution is by the TM field
    )
    sold = await day.sold_deals_by_tm(bx, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    assert sold == {5: 1, 8: 1}  # deal 21→TM 5 (once), 22→TM 8; 23 (TM 0) dropped


# --- funnel block ------------------------------------------------------------


async def test_funnel_counts_and_cr() -> None:
    """Funnel stages + overall CR (arrived ÷ leads); the trend is loaded lazily."""
    bx = FakeBitrix(
        stage_owners={
            _QUALIFIED: [1, 2, 3],
            _MEETING_SET: [1, 2],
            _WON: [1],  # cat-24 «Фактический визит» → arrived
            _SOLD: [9],  # cat-2 «БРОНЬ ПОДПИСАН» → bought
            _NO_ANSWER[0]: [4, 5],  # deal 4 hit Недозвон 1 & 2 → counted once
            _NO_ANSWER[1]: [4],
        },
        assignee={1: 7, 2: 7, 3: 7, 4: 7, 5: 7},
        tm={1: "7", 9: "7"},  # deal 1 arrived, deal 9 bought, both this TM
        leads=10,
        # two deals closed with fail semantics in the period (reason split below)
        closed_lost_reasons={101: "1008", 102: "1008"},
        reason_items={1008: "Долгое принятие решения"},
    )
    funnel = await analytics._funnel(bx, 7, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    assert funnel.status == "live"
    counts = {s.key: s.count for s in funnel.stages}
    assert counts == {
        "leads": 10,
        "no_answer": 2,  # deals 4 & 5 (4 deduped across both Недозвон stages)
        "qualified": 3,
        "meeting_set": 2,
        "arrived": 1,
        "bought": 1,  # live: cat-2 booking attributed via «Сотрудник ТМ»
        "closed_lost": 2,
    }
    # Overall CR stays arrived ÷ leads — the leakage bars don't enter the chain.
    assert funnel.overall_cr_pct == round(1 / 10 * 100, 1)
    # The trend is split into /analytics/cr-trend (heavy) — never in the funnel.
    assert funnel.trend == []


class FakeTrendBitrix:
    """One combined WON pull (with CREATED_TIME) + per-month leads counts.

    ``won_events`` are ``(deal_id, "YYYY-MM-DD…")`` stage-history rows; ``tm``
    maps a deal to its «Сотрудник ТМ»; ``leads_by_month`` is the deal-creation
    count returned per month (keyed ``YYYY-MM``).
    """

    def __init__(
        self,
        *,
        won_events: list[tuple[int, str]],
        tm: dict[int, Any],
        leads_by_month: dict[str, int],
    ) -> None:
        self.won_events = won_events
        self.tm = tm
        self.leads_by_month = leads_by_month
        self.won_pulls = 0

    async def call_raw(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Single WON history page (with CREATED_TIME) and per-month leads count."""
        params = params or {}
        if method == "crm.stagehistory.list":
            self.won_pulls += 1
            items = [{"OWNER_ID": d, "CREATED_TIME": t} for d, t in self.won_events]
            return {"result": {"items": items}}
        if method == "crm.deal.list":  # leads count for one month
            since = str((params.get("filter") or {}).get(">=DATE_CREATE", ""))[:7]
            return {"result": [], "total": self.leads_by_month.get(since, 0)}
        raise AssertionError(f"unexpected call_raw {method}")

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Deal id-lookup rows carrying «Сотрудник ТМ»."""
        assert method == "crm.deal.list"
        for deal_id in (params or {})["filter"]["ID"]:
            yield {"ID": str(deal_id), _FIELD: self.tm.get(int(deal_id))}


async def test_cr_trend_combined_pull_buckets_by_month() -> None:
    """CR trend uses ONE WON pull bucketed by month; arrived attributed by TM."""
    bx = FakeTrendBitrix(
        won_events=[
            (1, "2020-01-10T00:00:00+03:00"),  # uid 7, Jan
            (2, "2020-01-20T00:00:00+03:00"),  # uid 7, Jan
            (3, "2019-12-15T00:00:00+03:00"),  # uid 7, Dec
            (4, "2020-01-05T00:00:00+03:00"),  # other TM — excluded
        ],
        tm={1: "7", 2: "7", 3: "7", 4: "999"},
        leads_by_month={"2020-01": 10, "2019-12": 5},
    )
    points = await analytics._cr_trend(bx, 7, _PAST_START)  # type: ignore[arg-type]
    assert bx.won_pulls == 1  # single combined history pull, not one per month
    assert len(points) == settings.companion_analytics_trend_months
    by_period = {p.period: p.cr_pct for p in points}
    assert by_period["2020-01"] == round(2 / 10 * 100, 1)  # deals 1 & 2
    assert by_period["2019-12"] == round(1 / 5 * 100, 1)  # deal 3 (deal 4 excluded)


async def test_closed_lost_breaks_down_by_reason() -> None:
    """«Закрыто (отказ)» splits by the отказ-причина enum, largest first.

    Labels come from crm.deal.fields; a deal with no reason falls into «Не
    указана» (reason_id None); breakdown counts sum to the stage total.
    """
    bx = FakeBitrix(
        leads=5,
        closed_lost_reasons={1: "1008", 2: "1008", 3: "2607", 4: None},
        reason_items={
            1008: "Долгое принятие решения",
            2607: "Нет одобрения по ипотеке",
        },
    )
    funnel = await analytics._funnel(bx, 7, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    closed = next(s for s in funnel.stages if s.key == "closed_lost")
    assert closed.count == 4
    assert closed.breakdown is not None
    assert [(r.label, r.count) for r in closed.breakdown] == [
        ("Долгое принятие решения", 2),
        ("Не указана", 1),
        ("Нет одобрения по ипотеке", 1),
    ]
    by_label = {r.label: r.reason_id for r in closed.breakdown}
    assert by_label["Долгое принятие решения"] == "1008"
    assert by_label["Не указана"] is None
    # breakdown counts reconcile with the bar total
    assert sum(r.count for r in closed.breakdown) == closed.count


async def test_funnel_empty_is_not_available() -> None:
    """No leads and no stage activity → honest not_available."""
    bx = FakeBitrix(leads=0)
    funnel = await analytics._funnel(bx, 7, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    assert funnel.status == "not_available"
    assert funnel.overall_cr_pct is None


# --- meetings block ----------------------------------------------------------


async def test_meetings_outcomes_with_reschedule() -> None:
    """назначено / дошли / переназначились / недошли; купили stays None."""
    bx = FakeBitrix(
        stage_owners={
            _MEETING_SET: [1, 1, 2, 3],  # deal 1 re-booked
            _NO_SHOW: [2],
            _WON: [3],  # arrived
            _SOLD: [3],  # deal 3 went on to a signed booking
        },
        assignee={1: 5, 2: 5, 3: 5},
        tm={3: "5"},
    )
    meetings = await analytics._meetings(bx, 5, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    assert meetings.status == "live"
    assert meetings.meetings_set == 3  # deals 1,2,3 (distinct)
    assert meetings.rescheduled == 1  # deal 1 entered twice
    assert meetings.no_show == 1
    assert meetings.arrived == 1
    assert meetings.bought == 1  # cat-2 booking signed, attributed to this TM


# --- tasks block -------------------------------------------------------------


async def test_tasks_past_period_routes_incomplete_to_overdue() -> None:
    """A past window: COMPLETED=N activities are overdue; «открыто» read skipped."""
    bx = FakeBitrix(closed=12, not_done=4)
    tasks = await analytics._tasks(bx, 5, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    assert tasks.status == "live"
    assert tasks.closed == 12
    assert tasks.overdue == 4
    assert tasks.pending == 0
    assert tasks.total == 16
    assert tasks.closed_on_time is None


async def test_tasks_future_period_routes_incomplete_to_open() -> None:
    """A future window: COMPLETED=N activities are «открыто», none overdue."""
    bx = FakeBitrix(closed=2, not_done=7)
    tasks = await analytics._tasks(bx, 5, _FUT_START, _FUT_END)  # type: ignore[arg-type]
    assert tasks.overdue == 0
    assert tasks.pending == 7
    assert tasks.closed == 2
    assert tasks.total == 9


# --- calls block -------------------------------------------------------------


async def test_calls_block_breaks_down_telephony() -> None:
    """Talk time sums answered; completed/no-answer/incoming counted by code/type."""
    bx = FakeBitrix(
        calls=[
            {"CALL_FAILED_CODE": "200", "CALL_DURATION": "120", "CALL_TYPE": "1"},
            {"CALL_FAILED_CODE": "200", "CALL_DURATION": 95, "CALL_TYPE": "2"},
            {"CALL_FAILED_CODE": "304", "CALL_DURATION": "999", "CALL_TYPE": "1"},
            {"CALL_FAILED_CODE": "200", "CALL_TYPE": "2"},  # incoming, no duration
        ],
    )
    calls = await analytics._calls(bx, 5, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    assert calls.status == "live"
    assert calls.completed == 3  # three answered (200)
    assert calls.no_answer == 1  # the 304
    assert calls.incoming == 2  # two CALL_TYPE=2
    assert calls.talk_time_sec == 215  # 120 + 95 (+0 for the missing duration)


# --- endpoint scoping --------------------------------------------------------

_TOKEN = "test-companion-token"
_MANAGER_KEY = "mgr-analytics-key"


@pytest.fixture
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "companion_api_token", _TOKEN)


async def _seed_manager_user(session: Any, bitrix_user_id: int) -> None:
    session.add(Manager(bitrix_user_id=bitrix_user_id, name="Олжас", last_name="М."))
    session.add(
        CompanionUser(
            key_sha256=hash_key(_MANAGER_KEY),
            role=CompanionRole.MANAGER,
            bitrix_user_id=bitrix_user_id,
            name="Олжас М.",
        ),
    )
    await session.flush()


async def test_analytics_endpoint_scopes_to_manager(
    client: AsyncClient,
    dbsession: Any,
    _token: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manager reads their own analytics (200) but not another's (403)."""
    await _seed_manager_user(dbsession, bitrix_user_id=701)
    headers = {
        "Authorization": f"Bearer {_TOKEN}",
        "X-Companion-User-Key": _MANAGER_KEY,
    }

    async def _fake_get(session: Any, uid: int, period: str | None) -> Any:
        from AtamuraOKK.web.api.v1.schemas import AnalyticsView, ManagerRef

        return AnalyticsView(manager=ManagerRef(bitrix_user_id=uid), period="2026-03")

    monkeypatch.setattr(analytics, "get_analytics", _fake_get)

    own = await client.get("/api/v1/managers/701/analytics", headers=headers)
    assert own.status_code == 200
    assert own.json()["manager"]["bitrix_user_id"] == 701

    other = await client.get("/api/v1/managers/999/analytics", headers=headers)
    assert other.status_code == 403


async def test_analytics_requires_user_key(client: AsyncClient, _token: None) -> None:
    """The service bearer alone (no personal key) is rejected."""
    resp = await client.get(
        "/api/v1/managers/1/analytics",
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 401


async def test_cr_trend_endpoint_scoped_and_lazy(
    client: AsyncClient,
    dbsession: Any,
    _token: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lazy CR-trend route is reachable, returns a list, and is role-scoped."""
    await _seed_manager_user(dbsession, bitrix_user_id=701)
    headers = {
        "Authorization": f"Bearer {_TOKEN}",
        "X-Companion-User-Key": _MANAGER_KEY,
    }

    async def _fake_trend(uid: int, period: str | None) -> Any:
        from AtamuraOKK.web.api.v1.schemas import AnalyticsTrendPoint

        return [AnalyticsTrendPoint(period="2026-03", cr_pct=12.0)]

    monkeypatch.setattr(analytics, "get_cr_trend", _fake_trend)

    own = await client.get("/api/v1/managers/701/analytics/cr-trend", headers=headers)
    assert own.status_code == 200
    assert own.json() == [{"period": "2026-03", "cr_pct": 12.0}]

    other = await client.get(
        "/api/v1/managers/999/analytics/cr-trend", headers=headers
    )
    assert other.status_code == 403
