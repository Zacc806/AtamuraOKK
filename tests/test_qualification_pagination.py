"""Qualification checker: full paging + the earliest qualification moment.

C3 regression kept: deals are paged in full (a client with >50 deals could
otherwise have the qualifying deal dropped). New contract: the checker returns
a Qualification with the *earliest* qualified-stage CREATED_TIME, read across
every stage-history page (the earliest entry can sit anywhere).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from AtamuraOKK.ingestion.qualification import (
    UNKNOWN_QUALIFICATION,
    ContactDealStageQualificationChecker,
    Qualification,
)

_STAGE_IDS = {"C2:QUALIFIED"}


def _entry(created: str) -> dict[str, str]:
    return {"STAGE_ID": "C2:QUALIFIED", "CREATED_TIME": created}


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


class _FakeBitrix:
    """Serves paged stage-history envelopes and records the filter params."""

    def __init__(
        self,
        *,
        deal_count: int,
        history_pages: list[dict[str, Any]],
    ) -> None:
        self._deal_count = deal_count
        self._pages = history_pages
        self.stagehistory_params: dict[str, Any] | None = None
        self.history_reads = 0

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Page out the client's deals."""
        assert method == "crm.deal.list"
        for i in range(self._deal_count):
            yield {"ID": i + 1}

    async def call_raw(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Serve the next canned stage-history page."""
        assert method == "crm.stagehistory.list"
        self.stagehistory_params = params
        page = self._pages[self.history_reads]
        self.history_reads += 1
        return page


async def test_qualified_uses_all_deals_and_earliest_time() -> None:
    """All deals are paged into the filter; the entry time becomes the moment."""
    # 120 deals (well past one 50-row page) and one qualifying entry.
    bx = _FakeBitrix(
        deal_count=120,
        history_pages=[{"result": {"items": [_entry("2026-06-01T10:00:00+03:00")]}}],
    )
    checker = ContactDealStageQualificationChecker(qualified_stage_ids=set(_STAGE_IDS))

    result = await checker.qualified({"CONTACT:999"}, bx)  # type: ignore[arg-type]

    expected = Qualification(qualified=True, at=_ts("2026-06-01T10:00:00+03:00"))
    assert result == {"CONTACT:999": expected}
    # All 120 deal ids were forwarded to the stage-history filter, not just 50.
    assert bx.stagehistory_params is not None
    assert len(bx.stagehistory_params["filter"]["OWNER_ID"]) == 120


async def test_earliest_found_across_history_pages() -> None:
    """The earliest entry on page 2 wins; every page is read."""
    bx = _FakeBitrix(
        deal_count=2,
        history_pages=[
            {"result": {"items": [_entry("2026-06-05T09:00:00+03:00")]}, "next": 50},
            {"result": {"items": [_entry("2026-05-20T08:00:00+03:00")]}},
        ],
    )
    checker = ContactDealStageQualificationChecker(qualified_stage_ids=set(_STAGE_IDS))

    result = await checker.qualified({"CONTACT:1"}, bx)  # type: ignore[arg-type]

    expected = Qualification(qualified=True, at=_ts("2026-05-20T08:00:00+03:00"))
    assert result == {"CONTACT:1": expected}
    assert bx.history_reads == 2


async def test_not_qualified_when_no_entries() -> None:
    """No qualified-stage entries -> not qualified, no moment."""
    bx = _FakeBitrix(deal_count=2, history_pages=[{"result": {"items": []}}])
    checker = ContactDealStageQualificationChecker(qualified_stage_ids=set(_STAGE_IDS))

    result = await checker.qualified({"CONTACT:1"}, bx)  # type: ignore[arg-type]

    assert result == {"CONTACT:1": Qualification(qualified=False)}


async def test_unresolvable_client_is_unknown() -> None:
    """Phone-only clients cannot be resolved to deals -> unknown."""
    bx = _FakeBitrix(deal_count=0, history_pages=[])
    checker = ContactDealStageQualificationChecker(qualified_stage_ids=set(_STAGE_IDS))

    result = await checker.qualified({"PHONE:+77001234567"}, bx)  # type: ignore[arg-type]

    assert result == {"PHONE:+77001234567": UNKNOWN_QUALIFICATION}
