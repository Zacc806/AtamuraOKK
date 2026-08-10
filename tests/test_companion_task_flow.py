"""Companion РОП «Задачи за день» — per-manager task flow across the day.

``day.team_task_flow`` reconstructs how many tasks each manager still had open at
each checkpoint (Bitrix keeps no such history), counts the day's new tasks and
sums talk time. These tests fake the Bitrix client and verify the filters it
issues, the created-before/closed-after reconstruction, the telephony exclusion,
and the truncation flag.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from AtamuraOKK.settings import settings
from AtamuraOKK.web.api.v1 import day

pytestmark = pytest.mark.anyio

_TZ = ZoneInfo(settings.report_timezone)
_DAY = datetime(2026, 7, 2, tzinfo=_TZ)
_DAY_END = _DAY + timedelta(days=1)
_CHECKPOINTS = [_DAY.replace(hour=h) for h in (10, 14, 18)]


def _at(hour: int, minute: int = 0) -> str:
    return _DAY.replace(hour=hour, minute=minute).isoformat()


class FakeTaskBitrix:
    """Replays the three activity scans plus the telephony scan, by filter shape."""

    def __init__(
        self,
        still_open: list[dict[str, Any]] | None = None,
        closed: list[dict[str, Any]] | None = None,
        created: list[dict[str, Any]] | None = None,
        calls: list[dict[str, Any]] | None = None,
    ) -> None:
        self.still_open = still_open or []
        self.closed = closed or []
        self.created = created or []
        self.calls = calls or []
        self.filters: dict[str, dict[str, Any]] = {}

    def _rows_for(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        flt = params.get("filter") or {}
        if flt.get("COMPLETED") == "N":
            self.filters["open"] = flt
            return self.still_open
        if flt.get("COMPLETED") == "Y":
            self.filters["closed"] = flt
            return self.closed
        self.filters["created"] = flt
        return self.created

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield the canned rows for whichever scan is being run."""
        params = params or {}
        if method == "voximplant.statistic.get":
            self.filters["calls"] = params.get("FILTER") or {}
            rows = self.calls
        else:
            assert method == "crm.activity.list"
            rows = self._rows_for(params)
        for i, row in enumerate(rows):
            if max_items is not None and i >= max_items:
                return
            yield row


def _task(uid: int, created: str, last_updated: str | None = None) -> dict[str, Any]:
    row = {"ID": "1", "RESPONSIBLE_ID": str(uid), "CREATED": created}
    if last_updated:
        row["LAST_UPDATED"] = last_updated
    return row


def _call(uid: int, seconds: int, failed_code: str | None = None) -> dict[str, Any]:
    return {
        "PORTAL_USER_ID": str(uid),
        "CALL_DURATION": str(seconds),
        "CALL_FAILED_CODE": failed_code or settings.ingest_success_code,
    }


async def _flow(bx: FakeTaskBitrix, uids: list[int]) -> tuple[dict[int, Any], bool]:
    return await day.team_task_flow(
        bx,  # type: ignore[arg-type]
        uids,
        _DAY,
        _DAY_END,
        _CHECKPOINTS,
        100,
    )


async def test_empty_roster_short_circuits() -> None:
    """No team members -> no Bitrix read at all."""
    bx = FakeTaskBitrix(still_open=[_task(5, _at(8))])
    counts, truncated = await _flow(bx, [])
    assert counts == {}
    assert truncated is False
    assert bx.filters == {}


async def test_still_open_task_counts_from_its_creation_on() -> None:
    """Created 12:00, never closed -> absent at 10:00, present at 14:00 and 18:00."""
    bx = FakeTaskBitrix(still_open=[_task(5, _at(12))])
    counts, _ = await _flow(bx, [5])
    assert counts[5].open_at == {10: 0, 14: 1, 18: 1}


async def test_closed_task_stops_counting_after_it_is_closed() -> None:
    """Created yesterday, closed 13:00 -> open at 10:00 only."""
    yesterday = (_DAY - timedelta(days=1)).replace(hour=9).isoformat()
    bx = FakeTaskBitrix(closed=[_task(5, yesterday, _at(13))])
    counts, _ = await _flow(bx, [5])
    assert counts[5].open_at == {10: 1, 14: 0, 18: 0}


async def test_task_closed_exactly_on_a_checkpoint_still_counts_there() -> None:
    """Closed at 14:00 sharp -> it was still open when the 14:00 срез was taken."""
    bx = FakeTaskBitrix(closed=[_task(5, _at(9), _at(14))])
    counts, _ = await _flow(bx, [5])
    assert counts[5].open_at == {10: 1, 14: 1, 18: 0}


async def test_backlog_declines_across_the_day() -> None:
    """The whole point of the table: остаток разгребается 3 -> 2 -> 1."""
    bx = FakeTaskBitrix(
        still_open=[_task(5, _at(8))],
        closed=[_task(5, _at(8), _at(11)), _task(5, _at(8), _at(15))],
    )
    counts, _ = await _flow(bx, [5])
    assert counts[5].open_at == {10: 3, 14: 2, 18: 1}


async def test_scans_exclude_telephony_and_scope_the_day() -> None:
    """Every scan drops the auto-logged call activities and stays in the day."""
    bx = FakeTaskBitrix(still_open=[_task(5, _at(8))])
    await _flow(bx, [5, 9])
    for key in ("open", "closed", "created"):
        assert bx.filters[key]["!PROVIDER_ID"] == day._TELEPHONY_PROVIDERS
        assert bx.filters[key]["RESPONSIBLE_ID"] == [5, 9]
    assert bx.filters["open"][">=DEADLINE"] == day._DEADLINE_FLOOR
    assert bx.filters["open"]["<DEADLINE"] == _DAY_END.isoformat()
    # only tasks touched at/after the first checkpoint can have been open at one
    assert bx.filters["closed"][">=LAST_UPDATED"] == _CHECKPOINTS[0].isoformat()
    assert bx.filters["created"][">=CREATED"] == _DAY.isoformat()
    assert bx.filters["created"]["<CREATED"] == _DAY_END.isoformat()


async def test_counts_new_tasks_and_talk_time_per_manager() -> None:
    """«Новых» and «время на линии» are attributed per manager, others zeroed."""
    bx = FakeTaskBitrix(
        created=[_task(5, _at(9)), _task(5, _at(16)), _task(9, _at(10))],
        calls=[_call(5, 120), _call(5, 300), _call(9, 60)],
    )
    counts, _ = await _flow(bx, [5, 9, 12])
    assert (counts[5].created, counts[5].talk_seconds) == (2, 420)
    assert (counts[9].created, counts[9].talk_seconds) == (1, 60)
    assert (counts[12].created, counts[12].talk_seconds) == (0, 0)
    assert counts[12].open_at == {10: 0, 14: 0, 18: 0}


async def test_unanswered_calls_do_not_count_as_time_on_the_line() -> None:
    """Only answered calls (success code) add to talk time."""
    bx = FakeTaskBitrix(calls=[_call(5, 120), _call(5, 45, failed_code="304")])
    counts, _ = await _flow(bx, [5])
    assert counts[5].talk_seconds == 120


async def test_rows_of_other_managers_are_ignored() -> None:
    """A row Bitrix returns outside the roster never lands on someone else."""
    bx = FakeTaskBitrix(still_open=[_task(7, _at(8))], created=[_task(7, _at(8))])
    counts, _ = await _flow(bx, [5])
    assert set(counts) == {5}
    assert counts[5].open_at == {10: 0, 14: 0, 18: 0}
    assert counts[5].created == 0


async def test_hitting_the_scan_cap_flags_truncation() -> None:
    """More rows than the cap -> counts under-report, truncated=True."""
    bx = FakeTaskBitrix(still_open=[_task(5, _at(8)) for _ in range(5)])
    counts, truncated = await day.team_task_flow(
        bx,  # type: ignore[arg-type]
        [5],
        _DAY,
        _DAY_END,
        _CHECKPOINTS,
        3,
    )
    assert truncated is True
    assert counts[5].open_at == {10: 3, 14: 3, 18: 3}


async def test_no_elapsed_checkpoints_short_circuits() -> None:
    """A day that hasn't reached its first срез yet reads nothing."""
    bx = FakeTaskBitrix(still_open=[_task(5, _at(8))])
    counts, truncated = await day.team_task_flow(
        bx,  # type: ignore[arg-type]
        [5],
        _DAY,
        _DAY_END,
        [],
        100,
    )
    assert counts == {}
    assert truncated is False
    assert bx.filters == {}
