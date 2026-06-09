"""Minimal async client for the Bitrix24 inbound-webhook REST API.

Only the pieces this project needs: a single-method call, transparent
pagination over list methods, and backoff on Bitrix's rate limiter.

Reference: https://apidocs.bitrix24.com/api-reference/how-to-call-rest-api/
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Self

import httpx
from loguru import logger

from AtamuraOKK.settings import settings

# Bitrix returns at most this many rows per list-method page.
PAGE_SIZE = 50

# Error codes that mean "back off and retry", not "give up".
_RETRYABLE_CODES = frozenset(
    {"QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT", "INTERNAL_SERVER_ERROR"},
)

# HTTP statuses that mean "back off and retry" (throttle / transient server error).
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header (delta-seconds form only)."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class BitrixError(RuntimeError):
    """A Bitrix REST call returned an ``error`` payload."""

    def __init__(self, code: str, description: str, method: str) -> None:
        self.code = code
        self.description = description
        self.method = method
        super().__init__(f"{method}: {code} - {description}")


class BitrixClient:
    """Async wrapper around a Bitrix24 inbound webhook.

    Usage::

        async with BitrixClient() as bx:
            me = await bx.call("profile")
            async for call in bx.list("voximplant.statistic.get", filter=...):
                ...
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = 60.0,
    ) -> None:
        base = (base_url or settings.bitrix_base).rstrip("/") + "/"
        if "/rest/" not in base:
            raise ValueError(
                "Bitrix webhook URL looks wrong (no '/rest/' segment); "
                "set BITRIX_WEBHOOK in .env to the full inbound-webhook URL.",
            )
        self._base = base
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()

    async def _invoke(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST one method and return its parsed envelope, with backoff.

        Retries on transport errors, retryable HTTP statuses (429/5xx), and
        Bitrix throttling error codes; guards a non-JSON body (an HTML throttle
        page) so it surfaces as a ``BitrixError`` instead of crashing the caller.
        """
        url = f"{self._base}{method}.json"
        payload = params or {}
        delay = settings.bitrix_retry_base_delay
        last = settings.bitrix_max_retries

        for attempt in range(1, last + 1):
            try:
                response = await self._client.post(url, json=payload)
            except httpx.HTTPError as exc:
                if attempt < last:
                    await self._backoff(method, f"transport: {exc}", attempt, delay)
                    delay *= 2
                    continue
                raise BitrixError("TRANSPORT_ERROR", str(exc), method) from exc

            status = response.status_code
            if status in _RETRYABLE_STATUS and attempt < last:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                wait = retry_after if retry_after is not None else delay
                await self._backoff(method, f"HTTP {status}", attempt, wait)
                delay *= 2
                continue
            if status >= 400:
                raise BitrixError(f"HTTP_{status}", response.text[:500], method)

            try:
                data: dict[str, Any] = response.json()
            except ValueError as exc:
                raise BitrixError(
                    "INVALID_JSON",
                    f"non-JSON response: {response.text[:200]}",
                    method,
                ) from exc

            if "error" not in data:
                return data

            code = str(data.get("error", "")).upper()
            description = str(data.get("error_description", ""))
            if code in _RETRYABLE_CODES and attempt < last:
                await self._backoff(method, code, attempt, delay)
                delay *= 2
                continue
            raise BitrixError(code, description, method)

        raise BitrixError("RETRIES_EXHAUSTED", "max retries reached", method)

    async def _backoff(self, method: str, why: str, attempt: int, delay: float) -> None:
        logger.warning(
            "Bitrix {method} retrying ({why}); attempt {n} in {d}s",
            method=method,
            why=why,
            n=attempt,
            d=delay,
        )
        await asyncio.sleep(delay)

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call one REST method and return its ``result`` field."""
        data = await self._invoke(method, params)
        return data.get("result")

    async def call_raw(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call a list method and return the full envelope (with ``next``/``total``)."""
        return await self._invoke(method, params)

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every row of a Bitrix list method, paging via the ``start`` cursor.

        :param method: e.g. ``voximplant.statistic.get``.
        :param params: FILTER/ORDER/SELECT dict (without ``start``).
        :param max_items: stop after this many rows (None = all).
        """
        params = dict(params or {})
        start = 0
        yielded = 0
        while True:
            params["start"] = start
            envelope = await self.call_raw(method, params)
            rows = envelope.get("result") or []
            if not rows:
                return
            for row in rows:
                yield row
                yielded += 1
                if max_items is not None and yielded >= max_items:
                    return
            next_start = envelope.get("next")
            if next_start is None:
                return
            start = int(next_start)
