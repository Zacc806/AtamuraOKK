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

# Transient HTTP statuses that warrant a retry.
_HTTP_TOO_MANY = 429
_HTTP_SERVER_ERROR = 500


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

    async def _request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST one method with exponential backoff; return the full envelope.

        Retries on Bitrix throttling error codes AND transient HTTP statuses
        (429 / 5xx), so high-volume paging via :meth:`list` survives rate limits.
        """
        url = f"{self._base}{method}.json"
        payload = params or {}
        delay = settings.bitrix_retry_base_delay

        for attempt in range(1, settings.bitrix_max_retries + 1):
            last = attempt == settings.bitrix_max_retries
            response = await self._client.post(url, json=payload)

            if response.status_code == _HTTP_TOO_MANY or (
                response.status_code >= _HTTP_SERVER_ERROR
            ):
                if last:
                    raise BitrixError(
                        f"HTTP_{response.status_code}",
                        response.text[:200],
                        method,
                    )
                logger.warning(
                    "Bitrix {method} HTTP {s}; retry {n} in {d}s",
                    method=method,
                    s=response.status_code,
                    n=attempt,
                    d=delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
                continue

            data: dict[str, Any] = response.json()
            if "error" not in data:
                return data

            code = str(data.get("error", "")).upper()
            description = str(data.get("error_description", ""))
            if code in _RETRYABLE_CODES and not last:
                logger.warning(
                    "Bitrix {method} throttled ({code}); retry {n} in {d}s",
                    method=method,
                    code=code,
                    n=attempt,
                    d=delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise BitrixError(code, description, method)

        raise BitrixError("RETRIES_EXHAUSTED", "max retries reached", method)

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call one REST method and return its ``result`` field (with retry)."""
        data = await self._request(method, params)
        return data.get("result")

    async def call_raw(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call a list method and return the full envelope (with retry)."""
        return await self._request(method, params)

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
