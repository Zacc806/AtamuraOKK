"""Companion /day money axis — conducted-meeting attribution via stage history.

A deal never rests at the meeting stage (it is moved to cat 2 and reassigned to
the closer at the moment of the visit), so meetings are counted from
crm.stagehistory.list WON transitions joined to the deal's «Сотрудник ТМ»
employee field. These tests fake the Bitrix client and verify the join, the
paging cursor, dedupe, the shared period cache, and the money-axis statuses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from AtamuraOKK.bitrix import BitrixError
from AtamuraOKK.settings import settings
from AtamuraOKK.web.api.v1 import day

pytestmark = pytest.mark.anyio

_FIELD = settings.companion_tm_employee_field
_START = datetime(2026, 6, 1, tzinfo=UTC)
_END = datetime(2026, 7, 1, tzinfo=UTC)
_PAGE = 50


@pytest.fixture(autouse=True)
def _fresh_caches() -> None:
    day._cache.clear()
    day._meetings_cache.clear()
    day._entrants_cache.clear()


class FakeBitrix:
    """Replays the two reads _money makes: stage history and deal lookups."""

    def __init__(
        self,
        won_deal_ids: list[int],
        tm_by_deal: dict[int, Any],
        leads_total: int = 0,
    ) -> None:
        self.won_deal_ids = won_deal_ids
        self.tm_by_deal = tm_by_deal
        self.leads_total = leads_total
        self.calls: list[str] = []

    async def call_raw(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Paged stage-history envelopes and the leads-count envelope."""
        self.calls.append(method)
        params = params or {}
        if method == "crm.stagehistory.list":
            start = int(params.get("start") or 0)
            chunk = self.won_deal_ids[start : start + _PAGE]
            items = [{"OWNER_ID": i} for i in chunk]
            env: dict[str, Any] = {"result": {"items": items}}
            if start + _PAGE < len(self.won_deal_ids):
                env["next"] = start + _PAGE
            return env
        if method == "crm.deal.list":  # the leads counter
            return {"result": [], "total": self.leads_total}
        raise AssertionError(f"unexpected method {method}")

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Deal rows for the TM-field lookup batches."""
        assert method == "crm.deal.list"
        self.calls.append(method)
        for deal_id in (params or {})["filter"]["ID"]:
            yield {"ID": str(deal_id), _FIELD: self.tm_by_deal.get(int(deal_id))}


async def test_meetings_attributed_via_tm_field() -> None:
    """WON history events count per TM through «Сотрудник ТМ», not assignee."""
    bx = FakeBitrix(
        # deal 4 hit WON twice -> distinct deals; deal 3 has no TM field
        won_deal_ids=[1, 2, 3, 4, 4],
        tm_by_deal={1: "68838", 2: "68838", 3: "0", 4: 64330},
        leads_total=158,
    )
    money = await day._money(bx, 68838, _START, _END)  # type: ignore[arg-type]
    assert money.status == "live"
    assert money.meetings == 2
    assert money.leads_processed == 158
    assert money.conversion_pct == round(2 / 158 * 100, 1)

    other = await day._money(bx, 64330, _START, _END)  # type: ignore[arg-type]
    assert other.meetings == 1


async def test_history_paging_follows_cursor() -> None:
    """More than one history page: every page is read and counted."""
    ids = list(range(1, 121))  # 120 deals -> 3 history pages
    bx = FakeBitrix(won_deal_ids=ids, tm_by_deal=dict.fromkeys(ids, "777"))
    counts = await day._meetings_by_tm(bx, _START, _END)  # type: ignore[arg-type]
    assert counts == {777: 120}
    assert bx.calls.count("crm.stagehistory.list") == 3


async def test_period_cache_shared_across_managers() -> None:
    """The second manager in the same period reuses the cached history pull."""
    bx = FakeBitrix(won_deal_ids=[1], tm_by_deal={1: "68838"}, leads_total=10)
    await day._money(bx, 68838, _START, _END)  # type: ignore[arg-type]
    history_reads = bx.calls.count("crm.stagehistory.list")
    await day._money(bx, 64330, _START, _END)  # type: ignore[arg-type]
    assert bx.calls.count("crm.stagehistory.list") == history_reads


async def test_no_leads_no_meetings_is_not_available() -> None:
    """Empty period stays honest: not_available, no invented conversion."""
    bx = FakeBitrix(won_deal_ids=[], tm_by_deal={})
    money = await day._money(bx, 68838, _START, _END)  # type: ignore[arg-type]
    assert money.status == "not_available"
    assert money.meetings == 0
    assert money.conversion_pct is None
    assert money.gates == {"plan_ok": False}


def test_build_actions_carries_bitrix_card_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each кому-звонить action deep-links to its deal's Bitrix CRM card."""
    monkeypatch.setattr(
        settings,
        "bitrix_webhook",
        "https://portal.bitrix24.kz/rest/1/tok/",
    )
    now = datetime(2026, 7, 2, tzinfo=UTC)
    actions = day._build_actions([{"ID": "42", "STAGE_ID": "NEW"}], {}, now)
    assert len(actions) == 1
    assert actions[0].deal_id == 42
    assert actions[0].bitrix_url == "https://portal.bitrix24.kz/crm/deal/details/42/"


def test_build_actions_tags_queue_like_stats() -> None:
    """Queue mirrors DayStats buckets: Недозвон→no_answer, no-show & stale→cooling."""
    now = datetime(2026, 7, 2, tzinfo=UTC)
    fresh, stale = now.isoformat(), (now - timedelta(days=30)).isoformat()
    # 1 = Недозвон stage, 2 = no-show stage, 3 = neutral+stale, 4 = neutral+fresh.
    deals = [
        {"ID": "1", "STAGE_ID": "C24:UC_VL3EHH", "LAST_ACTIVITY_TIME": fresh},
        {"ID": "2", "STAGE_ID": "C24:UC_9OBT14", "LAST_ACTIVITY_TIME": fresh},
        {"ID": "3", "STAGE_ID": "C24:PREPARATION", "LAST_ACTIVITY_TIME": stale},
        {"ID": "4", "STAGE_ID": "C24:PREPARATION", "LAST_ACTIVITY_TIME": fresh},
    ]
    q = {a.deal_id: a.queue for a in day._build_actions(deals, {}, now)}
    assert q == {1: "no_answer", 2: "cooling", 3: "cooling", 4: None}


def test_select_action_deals_keeps_examples_per_queue() -> None:
    """Stalest-first slicing alone starves no_answer; selection keeps a few."""
    now = datetime(2026, 7, 2, tzinfo=UTC)
    old, fresh = (now - timedelta(days=30)).isoformat(), now.isoformat()
    # 10 stale (cooling) deals sort ahead of the 2 freshly-active no_answer deals.
    deals = [
        {"ID": str(i), "STAGE_ID": "C24:PREPARATION", "LAST_ACTIVITY_TIME": old}
        for i in range(10)
    ] + [
        {"ID": "100", "STAGE_ID": "C24:UC_VL3EHH", "LAST_ACTIVITY_TIME": fresh},
        {"ID": "101", "STAGE_ID": "C24:UC_VL3EHH", "LAST_ACTIVITY_TIME": fresh},
    ]
    chosen = {d["ID"] for d in day._select_action_deals(deals, now, cap=5)}
    assert {"100", "101"} <= chosen  # no_answer examples survive the cap
    assert len(chosen) == 5


def test_select_action_deals_orders_no_answer_freshest_first() -> None:
    """Missed calls surface freshest-first — hottest just-active lead to call back."""
    now = datetime(2026, 7, 2, tzinfo=UTC)
    stale = (now - timedelta(hours=6)).isoformat()
    fresh = (now - timedelta(minutes=5)).isoformat()
    # _open_deals hands us stalest-first; selection must flip no_answer.
    deals = [
        {"ID": "stale", "STAGE_ID": "C24:UC_VL3EHH", "LAST_ACTIVITY_TIME": stale},
        {"ID": "fresh", "STAGE_ID": "C24:UC_VL3EHH", "LAST_ACTIVITY_TIME": fresh},
    ]
    chosen = [d["ID"] for d in day._select_action_deals(deals, now, cap=5)]
    assert chosen == ["fresh", "stale"]


# --- «Без задачи» — брошенные карточки без запланированного дела ---------------


class FakeActivityBitrix:
    """Replays crm.activity.list rows for _deals_with_open_task (deals with a task)."""

    def __init__(self, task_owner_ids: set[int]) -> None:
        self.task_owner_ids = task_owner_ids
        self.filters: list[dict[str, Any]] = []

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Open-activity rows for the batches of deal owners queried."""
        assert method == "crm.activity.list"
        f = (params or {})["filter"]
        self.filters.append(f)
        wanted = set(f["OWNER_ID"])
        for oid in sorted(self.task_owner_ids & wanted):
            yield {"ID": str(oid * 10), "OWNER_ID": str(oid)}


async def test_deals_with_open_task_returns_deals_that_have_one() -> None:
    """Only deals with an open activity come back; the rest are «без задачи»."""
    bx = FakeActivityBitrix(task_owner_ids={1, 3})
    got = await day._deals_with_open_task(bx, {1, 2, 3})  # type: ignore[arg-type]
    assert got == {1, 3}
    f = bx.filters[0]
    assert f["COMPLETED"] == "N"
    assert f["OWNER_TYPE_ID"] == day._DEAL_OWNER_TYPE_ID


async def test_deals_with_open_task_batches_in_fifties() -> None:
    """The OWNER_ID filter is chunked 50 deals at a time."""
    ids = set(range(1, 121))  # 120 deals -> 3 batches
    bx = FakeActivityBitrix(task_owner_ids=ids)
    got = await day._deals_with_open_task(bx, ids)  # type: ignore[arg-type]
    assert got == ids
    assert len(bx.filters) == 3


def test_compute_stats_counts_no_task_deals() -> None:
    """no_task = open deals absent from the with-task set; independent of stage."""
    now = datetime(2026, 7, 2, tzinfo=UTC)
    fresh = now.isoformat()
    deals = [
        {"ID": "1", "STAGE_ID": "C24:UC_VL3EHH", "LAST_ACTIVITY_TIME": fresh},
        {"ID": "2", "STAGE_ID": "C24:PREPARATION", "LAST_ACTIVITY_TIME": fresh},
    ]
    stats = day._compute_stats(deals, now, with_task_ids={1})
    assert stats.no_answer == 1
    assert stats.no_task == 1  # deal 2 has no open task


def test_compute_stats_no_task_null_without_activity_read() -> None:
    """No with-task info -> no_task stays null (UI "—"), never a fake zero."""
    now = datetime(2026, 7, 2, tzinfo=UTC)
    deals = [{"ID": "1", "STAGE_ID": "C24:PREPARATION", "LAST_ACTIVITY_TIME": None}]
    assert day._compute_stats(deals, now).no_task is None


def test_select_action_deals_surfaces_neutral_no_task_deals() -> None:
    """A neutral fresh deal is dropped — unless it has no task, then it surfaces."""
    now = datetime(2026, 7, 2, tzinfo=UTC)
    fresh = now.isoformat()
    deals = [{"ID": "7", "STAGE_ID": "C24:PREPARATION", "LAST_ACTIVITY_TIME": fresh}]
    assert day._select_action_deals(deals, now, cap=5) == []  # no info -> dropped
    chosen = {
        d["ID"]
        for d in day._select_action_deals(deals, now, cap=5, with_task_ids=set())
    }
    assert chosen == {"7"}


def test_build_actions_flags_no_task() -> None:
    """Each action carries the no_task flag (orthogonal to its stage queue)."""
    now = datetime(2026, 7, 2, tzinfo=UTC)
    deals = [{"ID": "1", "STAGE_ID": "C24:NEW"}, {"ID": "2", "STAGE_ID": "C24:NEW"}]
    flags = {
        a.deal_id: a.no_task
        for a in day._build_actions(deals, {}, now, with_task_ids={1})
    }
    assert flags == {1: False, 2: True}


# --- «Важные цифры дня» (today block) ---------------------------------------

_MEETING_SET = settings.companion_meeting_set_stage_id


def test_hot_stages_derived_from_signal_map() -> None:
    """Дожать-до-встречи uses exactly the hot pre-booking stages."""
    assert set(day._HOT_STAGES) == {
        "C24:UC_OPEENZ",  # просил перезвонить
        "C24:PREPAYMENT_INVOIC",  # квалифицирован
        "C24:UC_9OBT14",  # не дошёл
    }


class FakeTodayBitrix:
    """Replays the reads the today block makes (activities, telephony, history)."""

    def __init__(
        self,
        *,
        planned_total: int = 0,
        overdue_total: int = 0,
        calls: list[dict[str, Any]] | None = None,
        booked_deal_ids: list[int] | None = None,
        assignee_by_deal: dict[int, Any] | None = None,
        fail_methods: set[str] | None = None,
    ) -> None:
        self.planned_total = planned_total
        self.overdue_total = overdue_total
        self._calls = calls or []
        self.booked_deal_ids = booked_deal_ids or []
        self.assignee_by_deal = assignee_by_deal or {}
        self.fail_methods = fail_methods or set()
        self.seen: list[str] = []
        self.activity_filters: list[dict[str, Any]] = []

    async def call_raw(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Activity-count envelope and paged stage-history envelopes."""
        self.seen.append(method)
        if method in self.fail_methods:
            raise BitrixError("ERROR", "boom", method)
        if method == "crm.activity.list":
            # Both planned and overdue filter COMPLETED='N'; the planned-calls
            # read additionally carries a TYPE_ID (call) — that distinguishes them.
            f = (params or {}).get("filter") or {}
            self.activity_filters.append(f)
            total = self.planned_total if "TYPE_ID" in f else self.overdue_total
            return {"result": [], "total": total}
        if method == "crm.stagehistory.list":
            start = int((params or {}).get("start") or 0)
            chunk = self.booked_deal_ids[start : start + _PAGE]
            items = [{"OWNER_ID": i} for i in chunk]
            env: dict[str, Any] = {"result": {"items": items}}
            if start + _PAGE < len(self.booked_deal_ids):
                env["next"] = start + _PAGE
            return env
        raise AssertionError(f"unexpected call_raw {method}")

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Telephony rows (talk time) and deal rows (entrants assignee)."""
        self.seen.append(method)
        if method == "voximplant.statistic.get":
            for row in self._calls:
                yield row
            return
        if method == "crm.deal.list":  # entrants assignee lookup
            for deal_id in (params or {})["filter"]["ID"]:
                yield {
                    "ID": str(deal_id),
                    "ASSIGNED_BY_ID": self.assignee_by_deal.get(int(deal_id)),
                }
            return
        raise AssertionError(f"unexpected list {method}")


async def test_planned_calls_today_counts_only_open_calls() -> None:
    """Записано-на-сегодня counts only open call activities.

    ``COMPLETED='N'`` excludes the completed call-logs telephony auto-creates per
    real call — the inflation fix.
    """
    bx = FakeTodayBitrix(planned_total=17)
    start, end = day._today_window()
    assert await day._planned_calls_today(bx, 5, start, end) == 17  # type: ignore[arg-type]
    assert bx.seen == ["crm.activity.list"]
    flt = bx.activity_filters[0]
    assert flt["COMPLETED"] == "N"
    assert flt["TYPE_ID"] == settings.companion_call_activity_type_id


async def test_overdue_tasks_counts_incomplete_past_deadline() -> None:
    """Просроченных = incomplete activities due today but already past (COMPLETED=N)."""
    bx = FakeTodayBitrix(planned_total=17, overdue_total=6)
    day_start, day_end = day._today_window()
    assert await day._overdue_tasks(bx, 5, day_start, day_end) == 6  # type: ignore[arg-type]


async def test_talk_time_sums_answered_only() -> None:
    """Время-на-линии sums CALL_DURATION of answered calls, skips failed ones."""
    bx = FakeTodayBitrix(
        calls=[
            {"CALL_FAILED_CODE": "200", "CALL_DURATION": "120"},
            {"CALL_FAILED_CODE": "200", "CALL_DURATION": 95},
            {"CALL_FAILED_CODE": "304", "CALL_DURATION": "999"},  # not answered
            {"CALL_FAILED_CODE": "200"},  # missing duration -> 0
        ],
    )
    start, end = day._today_window()
    assert await day._talk_time_today(bx, 5, start, end) == 215  # type: ignore[arg-type]


async def test_meetings_set_counts_distinct_deals_by_assignee() -> None:
    """Назначено-сегодня: distinct deals entering the booking stage, per assignee."""
    bx = FakeTodayBitrix(
        booked_deal_ids=[10, 10, 11, 12],  # deal 10 re-entered -> distinct
        assignee_by_deal={10: "5", 11: 5, 12: "9"},
    )
    start, end = day._today_window()
    counts = await day._stage_entrants_by_assignee(
        bx,  # type: ignore[arg-type]
        _MEETING_SET,
        start,
        end,
    )
    assert counts == {5: 2, 9: 1}


async def test_push_counts_hot_stage_entrants_today() -> None:
    """Дожать-до-встречи: distinct deals entering any hot stage today, per assignee."""
    bx = FakeTodayBitrix(
        booked_deal_ids=[20, 20, 21],  # deal 20 re-entered -> distinct
        assignee_by_deal={20: 7, 21: "7"},
    )
    start, end = day._today_window()
    counts = await day._stage_entrants_by_assignee(
        bx,  # type: ignore[arg-type]
        day._HOT_STAGES,
        start,
        end,
    )
    assert counts == {7: 2}


async def test_today_metrics_resilient_to_partial_failure() -> None:
    """A failing sub-read degrades that one tile to None, others stay live."""
    bx = FakeTodayBitrix(
        planned_total=8,
        overdue_total=3,
        calls=[{"CALL_FAILED_CODE": "200", "CALL_DURATION": "60"}],
        booked_deal_ids=[10],
        assignee_by_deal={10: "5"},
        fail_methods={"crm.activity.list"},  # planned + overdue reads fail
    )
    today = await day._today_metrics(bx, 5, *day._today_window())  # type: ignore[arg-type]
    assert today.planned_calls is None  # failed read -> honest "—"
    assert today.overdue is None  # same crm.activity.list read failed
    assert today.talk_time_sec == 60  # telephony unaffected
    assert today.push_to_meeting == 1  # entered a hot stage today
    assert today.meetings_set == 1  # entered the booking stage today
    assert today.in_qual == 0  # no open deals passed -> nobody «в квале»


async def test_in_qual_counts_open_deals_at_qualified_stage() -> None:
    """«Дожать до встречи» = open deals resting at the qualified stage right now."""
    qual = settings.companion_qualified_stage_id
    deals = [
        {"ID": "1", "STAGE_ID": qual},
        {"ID": "2", "STAGE_ID": qual},
        {"ID": "3", "STAGE_ID": "C24:EXECUTING"},  # already booked -> not in qual
        {"ID": "4", "STAGE_ID": None},
    ]
    bx = FakeTodayBitrix()
    today = await day._today_metrics(  # type: ignore[arg-type]
        bx, 5, *day._today_window(), deals
    )
    assert today.in_qual == 2


def test_day_window_defaults_to_today() -> None:
    """No ``date`` -> today's window, labelled so it never collides with a date."""
    start, end, label = day._day_window(None)
    assert label == "today"
    assert (start, end) == day._today_window()


def test_day_window_parses_a_past_day() -> None:
    """A YYYY-MM-DD ``date`` -> that single day's window, labelled with the date."""
    start, end, label = day._day_window("2026-06-15")
    assert label == "2026-06-15"
    assert start.date().isoformat() == "2026-06-15"
    assert (end - start).days == 1


@pytest.mark.parametrize("bad", ["2026-06", "2026-06-01..2026-06-07", "nonsense"])
def test_day_window_rejects_non_day_specs(bad: str) -> None:
    """Only a single day is a valid ``date`` — months/ranges raise PeriodError."""
    from AtamuraOKK.web.api.v1.okk import PeriodError

    with pytest.raises(PeriodError):
        day._day_window(bad)


async def test_overdue_tasks_future_day_is_zero() -> None:
    """A day entirely in the future has nothing overdue yet — short-circuits to 0."""
    bx = FakeTodayBitrix(planned_total=17, overdue_total=6)
    start = datetime(2099, 1, 1, tzinfo=UTC)
    end = datetime(2099, 1, 2, tzinfo=UTC)
    assert await day._overdue_tasks(bx, 5, start, end) == 0  # type: ignore[arg-type]
    assert "crm.activity.list" not in bx.seen
