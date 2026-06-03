"""Tests for the Bitrix client retry + pagination."""

from __future__ import annotations

import json

import httpx
import pytest

from AtamuraOKK.bitrix.client import BitrixClient, BitrixError
from AtamuraOKK.settings import settings

BASE = "https://portal.bitrix24.kz/rest/1/token/"


def _client(handler: object) -> BitrixClient:
    """A BitrixClient whose transport is a MockTransport handler."""
    bx = BitrixClient(base_url=BASE)
    bx._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]  # noqa: SLF001
    return bx


async def test_call_raw_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 is retried with backoff until a 200 succeeds."""
    monkeypatch.setattr(settings, "bitrix_retry_base_delay", 0.0)
    monkeypatch.setattr(settings, "bitrix_max_retries", 3)
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, text="rate")
        return httpx.Response(200, json={"result": [{"ID": "1"}], "next": None})

    bx = _client(handler)
    envelope = await bx.call_raw("voximplant.statistic.get", {})
    await bx.aclose()
    assert calls["n"] == 3
    assert envelope["result"] == [{"ID": "1"}]


async def test_call_raises_on_nonretryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-retryable Bitrix error code raises BitrixError immediately."""
    monkeypatch.setattr(settings, "bitrix_retry_base_delay", 0.0)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": "INVALID_TOKEN", "error_description": "bad"},
        )

    bx = _client(handler)
    with pytest.raises(BitrixError):
        await bx.call("profile")
    await bx.aclose()


async def test_list_paginates_via_next_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list() follows the ``next`` cursor across pages, yielding every row."""
    monkeypatch.setattr(settings, "bitrix_retry_base_delay", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        start = json.loads(request.content or b"{}").get("start", 0)
        if start == 0:
            return httpx.Response(
                200,
                json={"result": [{"ID": "1"}, {"ID": "2"}], "next": 2},
            )
        return httpx.Response(200, json={"result": [{"ID": "3"}], "next": None})

    bx = _client(handler)
    rows = [row async for row in bx.list("voximplant.statistic.get", {})]
    await bx.aclose()
    assert [r["ID"] for r in rows] == ["1", "2", "3"]
