"""Companion РОП «Просроченные задачи» — team-wide overdue-task listing.

``day.team_overdue_tasks`` pulls incomplete Bitrix activities whose deadline has
already passed, across the whole team, oldest-due first. These tests fake the
Bitrix client and verify the filter/order it issues, the responsible-manager
attribution, the zero-date floor, and the cap/truncation flag.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from AtamuraOKK.web.api.v1 import day

pytestmark = pytest.mark.anyio

_NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


class FakeActivityBitrix:
    """Replays a single ``crm.activity.list`` read, honouring ``max_items``."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.filters: list[dict[str, Any]] = []
        self.orders: list[dict[str, Any]] = []

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield the canned activity rows, stopping at ``max_items``."""
        assert method == "crm.activity.list"
        params = params or {}
        self.filters.append(params.get("filter") or {})
        self.orders.append(params.get("order") or {})
        for i, row in enumerate(self.rows):
            if max_items is not None and i >= max_items:
                return
            yield row


def _row(activity_id: int, uid: int, deadline: str, subject: str = "Задача") -> dict:
    return {
        "ID": str(activity_id),
        "SUBJECT": subject,
        "DEADLINE": deadline,
        "OWNER_ID": str(activity_id * 10),
        "OWNER_TYPE_ID": "2",  # DEAL
        "RESPONSIBLE_ID": str(uid),
    }


async def test_empty_roster_short_circuits() -> None:
    """No team members -> no Bitrix read, empty result, not truncated."""
    bx = FakeActivityBitrix([_row(1, 5, "2026-07-01T09:00:00")])
    items, truncated = await day.team_overdue_tasks(
        bx,  # type: ignore[arg-type]
        {},
        250,
        "Керуен",
        _NOW,
        50,
    )
    assert items == []
    assert truncated is False
    assert bx.filters == []  # never queried Bitrix


async def test_issues_completed_deadline_filter_oldest_first() -> None:
    """Filter is COMPLETED=N, DEADLINE in [floor, now); order DEADLINE ASC."""
    bx = FakeActivityBitrix([_row(1, 5, "2026-07-01T09:00:00")])
    names = {5: "Иван Петров"}
    items, _ = await day.team_overdue_tasks(
        bx,  # type: ignore[arg-type]
        names,
        250,
        "Керуен",
        _NOW,
        50,
    )
    flt = bx.filters[0]
    assert flt["COMPLETED"] == "N"
    assert flt["RESPONSIBLE_ID"] == [5]
    assert flt[">=DEADLINE"] == day._DEADLINE_FLOOR
    assert flt["<DEADLINE"] == _NOW.isoformat()
    assert bx.orders[0] == {"DEADLINE": "ASC"}
    assert len(items) == 1
    task = items[0]
    assert task.activity_id == 1
    assert task.manager.bitrix_user_id == 5
    assert task.manager.name == "Иван Петров"
    assert task.manager.department_id == 250


async def test_attributes_each_task_to_its_responsible() -> None:
    """Team-wide: every row carries the manager it belongs to (by RESPONSIBLE_ID)."""
    rows = [
        _row(1, 5, "2026-06-20T09:00:00"),
        _row(2, 9, "2026-06-25T09:00:00"),
        _row(3, 5, "2026-07-01T09:00:00"),
    ]
    names = {5: "Иван Петров", 9: "Пётр Смирнов"}
    bx = FakeActivityBitrix(rows)
    items, _ = await day.team_overdue_tasks(
        bx,  # type: ignore[arg-type]
        names,
        250,
        "Керуен",
        _NOW,
        50,
    )
    assert [t.manager.bitrix_user_id for t in items] == [5, 9, 5]
    assert [t.manager.name for t in items] == [
        "Иван Петров",
        "Пётр Смирнов",
        "Иван Петров",
    ]
    assert sorted(bx.filters[0]["RESPONSIBLE_ID"]) == [5, 9]


async def test_caps_and_flags_truncation() -> None:
    """More matches than the cap -> list trimmed to cap, truncated=True."""
    rows = [_row(i, 5, f"2026-06-{i:02d}T09:00:00") for i in range(1, 6)]
    bx = FakeActivityBitrix(rows)
    items, truncated = await day.team_overdue_tasks(
        bx,  # type: ignore[arg-type]
        {5: "Иван Петров"},
        250,
        "Керуен",
        _NOW,
        3,
    )
    assert truncated is True
    assert len(items) == 3
    assert [t.activity_id for t in items] == [1, 2, 3]  # oldest first, capped


async def test_no_match_is_not_truncated() -> None:
    """Fewer matches than the cap -> not truncated."""
    bx = FakeActivityBitrix([_row(1, 5, "2026-07-01T09:00:00")])
    items, truncated = await day.team_overdue_tasks(
        bx,  # type: ignore[arg-type]
        {5: "Иван Петров"},
        250,
        "Керуен",
        _NOW,
        50,
    )
    assert len(items) == 1
    assert truncated is False
