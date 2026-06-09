"""C5 regression: Bitrix client HTTP hardening.

The client must guard a non-JSON body (an HTML throttle page), retry on
HTTP 429/5xx, and never let a raw JSONDecodeError escape and crash an ingestion
pass.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.settings import settings

_BASE = "http://example/rest/1/token/"


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep retry tests instant."""
    monkeypatch.setattr(settings, "bitrix_retry_base_delay", 0.0)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> BitrixClient:
    bx = BitrixClient(base_url=_BASE)
    bx._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    return bx


async def test_retries_on_429_then_succeeds() -> None:
    """A 429 backs off and the retry's 200 result is returned."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"result": 42})

    bx = _client(handler)
    try:
        assert await bx.call("voximplant.statistic.get") == 42
        assert calls["n"] == 2
    finally:
        await bx.aclose()


async def test_non_json_body_raises_bitrixerror_not_valueerror() -> None:
    """An HTML throttle page surfaces as a BitrixError, not a raw JSONDecodeError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>rate limited</html>")

    bx = _client(handler)
    try:
        with pytest.raises(BitrixError) as exc:
            await bx.call("any.method")
        assert exc.value.code == "INVALID_JSON"
    finally:
        await bx.aclose()


async def test_persistent_5xx_raises_bitrixerror() -> None:
    """A 5xx that never recovers raises BitrixError rather than crashing."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    bx = _client(handler)
    try:
        with pytest.raises(BitrixError):
            await bx.call("any.method")
    finally:
        await bx.aclose()


async def test_retryable_error_code_then_success() -> None:
    """A retryable Bitrix error code backs off, then the retry succeeds."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"error": "QUERY_LIMIT_EXCEEDED"})
        return httpx.Response(200, json={"result": "ok"})

    bx = _client(handler)
    try:
        assert await bx.call("any.method") == "ok"
        assert calls["n"] == 2
    finally:
        await bx.aclose()
