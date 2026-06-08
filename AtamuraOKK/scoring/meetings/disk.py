"""Bitrix Disk source for ОП meeting recordings — self-contained.

Walks the "Встречи ОП" Disk folder (the МОПs' personal disks were consolidated
there) and yields only the **audio/video** files — the actual meeting recordings —
skipping the scans/photos/docs that share the dump. Uses its own minimal Bitrix
REST helper (config-driven webhook), so this automation never imports the
call-pipeline ``AtamuraOKK.bitrix`` / ``AtamuraOKK.settings`` modules.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Self

import httpx
from loguru import logger

from AtamuraOKK.scoring.meetings.config import config

_RETRYABLE = frozenset(
    {"QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT", "INTERNAL_SERVER_ERROR"},
)

# Filename → meeting timestamp heuristics. The dump's folder names (month) are
# unreliable; filenames carry the real date, e.g.
# "WhatsApp Audio 2025-09-05 at 11.05.22.mp4".
_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+at\s+(\d{2})\.(\d{2})\.(\d{2})"),
    re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _T](\d{2})[.\-:](\d{2})[.\-:](\d{2})"),
    re.compile(r"(\d{4})(\d{2})(\d{2})[ _\-T]?(\d{2})(\d{2})(\d{2})"),
)
_DATE_ONLY = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


class BitrixDiskError(RuntimeError):
    """A Bitrix Disk REST call returned an ``error`` payload."""


@dataclass(slots=True)
class MeetingFile:
    """One meeting recording found on the Disk."""

    file_id: int
    name: str
    ext: str
    size: int
    folder_path: str  # human path under the root, for traceability
    download_url: str | None
    created_at: str | None  # Bitrix CREATE_TIME (when dumped into the folder)
    meeting_at: datetime | None  # parsed from the filename (real meeting time)


def parse_meeting_time(name: str) -> datetime | None:
    """Best-effort meeting timestamp from a recording's filename (None if absent)."""
    for pat in _DATE_PATTERNS:
        m = pat.search(name)
        if m:
            y, mo, d, h, mi, s = (int(g) for g in m.groups())
            try:
                return datetime(y, mo, d, h, mi, s)
            except ValueError:
                continue
    m = _DATE_ONLY.search(name)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return datetime(y, mo, d)
        except ValueError:
            return None
    return None


class BitrixDisk:
    """Minimal async Bitrix Disk client (only the methods this source needs)."""

    def __init__(self, webhook: str | None = None, *, timeout: float = 60.0) -> None:
        base = (webhook or config.bitrix_webhook).rstrip("/") + "/"
        if "/rest/" not in base:
            raise ValueError(
                "Bitrix webhook URL looks wrong (no '/rest/' segment); set "
                "ATAMURAOKK_BITRIX_WEBHOOK in .env to the inbound-webhook URL.",
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

    async def _call_raw(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base}{method}.json"
        delay = config.score_retry_base_delay
        for attempt in range(1, config.score_max_retries + 1):
            resp = await self._client.post(url, json=params)
            data: dict[str, Any] = resp.json()
            if "error" not in data:
                return data
            code = str(data.get("error", "")).upper()
            if code in _RETRYABLE and attempt < config.score_max_retries:
                logger.warning(
                    "Bitrix {m} throttled ({c}); retry {n}", m=method, c=code, n=attempt
                )
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise BitrixDiskError(
                f"{method}: {data.get('error')} {data.get('error_description', '')}"
            )
        raise BitrixDiskError(f"{method}: retries exhausted")

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call one method and return its ``result``."""
        return (await self._call_raw(method, params or {})).get("result")

    async def children(self, folder_id: int) -> list[dict[str, Any]]:
        """Every child (folders + files) of a Disk folder, transparently paged."""
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            env = await self._call_raw(
                "disk.folder.getchildren",
                {"id": folder_id, "start": start},
            )
            rows = env.get("result") or []
            out.extend(rows)
            nxt = env.get("next")
            if nxt is None or not rows:
                return out
            start = int(nxt)


class MeetingDiskSource:
    """Yields meeting recordings (audio/video) from the "Встречи ОП" folder tree."""

    def __init__(self, disk: BitrixDisk, *, root_id: int | None = None) -> None:
        self._disk = disk
        self._root_id = (
            root_id if root_id is not None else config.meetings_disk_folder_id
        )
        self._exts = frozenset(e.lower() for e in config.meetings_audio_exts)
        self._max_depth = config.meetings_walk_max_depth

    def _is_recording(self, name: str) -> bool:
        return PurePosixPath(name).suffix.lower() in self._exts

    async def iter_recordings(
        self,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[MeetingFile]:
        """Walk the folder tree and yield each audio/video recording once."""
        yielded = 0
        # Each stack entry holds a folder id, its human path, and its depth.
        stack: list[tuple[int, str, int]] = [(self._root_id, "", 0)]
        while stack:
            fid, path, depth = stack.pop()
            children = await self._disk.children(fid)
            for child in children:
                ctype = child.get("TYPE")
                name = str(child.get("NAME", ""))
                if ctype == "folder":
                    if depth < self._max_depth:
                        stack.append((int(child["ID"]), f"{path}/{name}", depth + 1))
                    continue
                if ctype != "file" or not self._is_recording(name):
                    continue
                yield MeetingFile(
                    file_id=int(child["ID"]),
                    name=name,
                    ext=PurePosixPath(name).suffix.lower(),
                    size=int(child.get("SIZE") or 0),
                    folder_path=path.lstrip("/"),
                    download_url=child.get("DOWNLOAD_URL"),
                    created_at=child.get("CREATE_TIME"),
                    meeting_at=parse_meeting_time(name),
                )
                yielded += 1
                if max_items is not None and yielded >= max_items:
                    return
