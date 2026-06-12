"""Companion /day money axis — conducted-meeting attribution via stage history.

A deal never rests at the meeting stage (it is moved to cat 2 and reassigned to
the closer at the moment of the visit), so meetings are counted from
crm.stagehistory.list WON transitions joined to the deal's «Сотрудник ТМ»
employee field. These tests fake the Bitrix client and verify the join, the
paging cursor, dedupe, the shared period cache, and the money-axis statuses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

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
