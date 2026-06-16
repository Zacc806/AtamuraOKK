"""Tests for the Bitrix deal-based client-category checker (no DB / no live Bitrix)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from AtamuraOKK.bitrix import BitrixError
from AtamuraOKK.ingestion.category import BitrixDealCategoryChecker

_FIELD = "UF_CRM_CAT"
_VALUE_MAP = {"1006": "A", "1008": "B", "1010": "C", "1012": "X"}


class _FakeBitrix:
    """Serves canned ``crm.deal.list`` pages and records the filters it received."""

    def __init__(self, deals_by_filter: dict[str, list[dict[str, Any]]]) -> None:
        # keyed by the single filter key=value, e.g. "CONTACT_ID=1"
        self._deals = deals_by_filter
        self.filters: list[dict[str, Any]] = []

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        assert method == "crm.deal.list"
        filter_ = (params or {}).get("filter", {})
        self.filters.append(filter_)
        ((key, value),) = filter_.items()
        for deal in self._deals.get(f"{key}={value}", []):
            yield deal


def _checker() -> BitrixDealCategoryChecker:
    return BitrixDealCategoryChecker(field=_FIELD, value_map=_VALUE_MAP)


async def test_contact_deal_enum_maps_to_letter() -> None:
    """A contact's deal enum-id is mapped through value_map to its letter."""
    bx = _FakeBitrix({"CONTACT_ID=123": [{"ID": "5", _FIELD: "1008"}]})
    result = await _checker().categorize({"CONTACT:123"}, bx)  # type: ignore[arg-type]
    assert result == {"CONTACT:123": "B"}


async def test_latest_deal_with_tag_wins() -> None:
    """Deals come newest-first; the first tagged deal (latest) is used."""
    bx = _FakeBitrix(
        {
            "CONTACT_ID=1": [
                {"ID": "30", _FIELD: ""},  # newest, untagged -> skipped
                {"ID": "20", _FIELD: "1006"},  # latest tagged -> A
                {"ID": "10", _FIELD: "1010"},  # older -> ignored
            ],
        },
    )
    result = await _checker().categorize({"CONTACT:1"}, bx)  # type: ignore[arg-type]
    assert result == {"CONTACT:1": "A"}


async def test_deal_entity_read_directly() -> None:
    """A DEAL-linked call reads its own deal."""
    bx = _FakeBitrix({"ID=77": [{"ID": "77", _FIELD: "1010"}]})
    result = await _checker().categorize({"DEAL:77"}, bx)  # type: ignore[arg-type]
    assert result == {"DEAL:77": "C"}
    assert bx.filters == [{"ID": "77"}]


async def test_lead_and_phone_are_none_without_bitrix_calls() -> None:
    """Unresolvable keys (no deal) -> None and never hit Bitrix."""
    bx = _FakeBitrix({})
    result = await _checker().categorize(
        {"PHONE:+77001234567", "LEAD:5"},
        bx,  # type: ignore[arg-type]
    )
    assert result == {"PHONE:+77001234567": None, "LEAD:5": None}
    assert bx.filters == []


async def test_unmapped_enum_is_none() -> None:
    """An enum id absent from value_map -> None (don't guess a letter)."""
    bx = _FakeBitrix({"CONTACT_ID=9": [{"ID": "1", _FIELD: "9999"}]})
    result = await _checker().categorize({"CONTACT:9"}, bx)  # type: ignore[arg-type]
    assert result == {"CONTACT:9": None}


async def test_no_tagged_deal_is_none() -> None:
    """A contact whose deals are all untagged -> None."""
    bx = _FakeBitrix({"CONTACT_ID=9": [{"ID": "1", _FIELD: ""}, {"ID": "2"}]})
    result = await _checker().categorize({"CONTACT:9"}, bx)  # type: ignore[arg-type]
    assert result == {"CONTACT:9": None}


async def test_bitrix_error_is_none() -> None:
    """A Bitrix error for one client degrades to None, not a crash."""

    class _Boom:
        def list(self, *_a: Any, **_k: Any) -> AsyncIterator[dict[str, Any]]:
            async def _gen() -> AsyncIterator[dict[str, Any]]:
                raise BitrixError("OOPS", "boom", "crm.deal.list")
                yield  # pragma: no cover - makes this an async generator

            return _gen()

    result = await _checker().categorize(
        {"CONTACT:1"},
        _Boom(),  # type: ignore[arg-type]
    )
    assert result == {"CONTACT:1": None}
