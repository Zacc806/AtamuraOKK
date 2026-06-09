"""C3 regression: qualification must not truncate at the first 50 rows.

Deals are paged in full (a client with >50 deals could otherwise have the
qualifying deal dropped), and the stage-history existence check uses the envelope
``total`` rather than only the first page of ``items``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from AtamuraOKK.ingestion.qualification import ContactDealStageQualificationChecker

_STAGE_IDS = {"C2:QUALIFIED"}


class _FakeBitrix:
    """Records the params it was called with so the test can assert no truncation."""

    def __init__(self, *, deal_count: int, stagehistory: dict[str, Any]) -> None:
        self._deal_count = deal_count
        self._stagehistory = stagehistory
        self.stagehistory_params: dict[str, Any] | None = None

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        assert method == "crm.deal.list"
        for i in range(self._deal_count):
            yield {"ID": i + 1}

    async def call_raw(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert method == "crm.stagehistory.list"
        self.stagehistory_params = params
        return self._stagehistory


async def test_qualified_uses_all_deals_and_total() -> None:
    """All deals are paged and a non-empty stage-history total marks qualified."""
    # 120 deals (well past one 50-row page) and a non-empty total => qualified.
    bx = _FakeBitrix(
        deal_count=120,
        stagehistory={"result": {"items": [{"STAGE_ID": "C2:QUALIFIED"}]}, "total": 3},
    )
    checker = ContactDealStageQualificationChecker(qualified_stage_ids=set(_STAGE_IDS))

    result = await checker.qualified({"CONTACT:999"}, bx)  # type: ignore[arg-type]

    assert result == {"CONTACT:999": True}
    # All 120 deal ids were forwarded to the stage-history filter, not just 50.
    assert bx.stagehistory_params is not None
    assert len(bx.stagehistory_params["filter"]["OWNER_ID"]) == 120


async def test_not_qualified_when_total_zero() -> None:
    """A zero stage-history total marks the client not qualified."""
    bx = _FakeBitrix(
        deal_count=2,
        stagehistory={"result": {"items": []}, "total": 0},
    )
    checker = ContactDealStageQualificationChecker(qualified_stage_ids=set(_STAGE_IDS))

    result = await checker.qualified({"CONTACT:1"}, bx)  # type: ignore[arg-type]

    assert result == {"CONTACT:1": False}


async def test_falls_back_to_items_when_total_absent() -> None:
    """When the envelope omits total, the items list decides existence."""
    bx = _FakeBitrix(
        deal_count=1,
        stagehistory={"result": {"items": [{"STAGE_ID": "C2:QUALIFIED"}]}},
    )
    checker = ContactDealStageQualificationChecker(qualified_stage_ids=set(_STAGE_IDS))

    result = await checker.qualified({"CONTACT:1"}, bx)  # type: ignore[arg-type]

    assert result == {"CONTACT:1": True}
