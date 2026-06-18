"""Minimal Auphonic API client for the A/B spike.

Uses the multi-step JSON API (create -> upload -> start -> poll -> download) so
we control the output format precisely: MP3 @ 192 kbps with ``mono_mixdown:false``
keeps the recording's channel count (stereo calls stay diarizable by channel)
while staying well under Yandex's 60 MB inline-recognition limit.

Cleanup algorithms applied: Adaptive Leveler, automatic Noise/Hum Reduction, and
high-pass Filtering. Loudness is normalized to -16 LUFS (speech target).

Auth is a Bearer token (``settings.auphonic_api_key``). Responses are wrapped as
``{"status_code": ..., "data": {...}}``; we unwrap ``data``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from AtamuraOKK.settings import settings

_BASE = "https://auphonic.com/api"
# Production lifecycle status codes (Auphonic): 3 = Done, 2 = Error.
_STATUS_DONE = 3
_STATUS_ERROR = 2

# Cleanup chain. denoiseamount 0 = automatic; loudnesstarget in LUFS.
_ALGORITHMS = {
    "leveler": True,
    "denoise": True,
    "denoiseamount": 0,
    "normloudness": True,
    "loudnesstarget": -16,
    "filtering": True,
}
# Keep codec parity with the original (MP3) and channel count (no mixdown).
_OUTPUT_FILES = [
    {"format": "mp3", "bitrate": "192", "mono_mixdown": False, "ending": "mp3"},
]


class AuphonicError(RuntimeError):
    """An Auphonic production failed or the API returned an error."""


@dataclass(slots=True)
class AuphonicResult:
    """Outcome of one production."""

    uuid: str
    output_path: Path
    status_string: str


class AuphonicClient:
    """Thin async wrapper over the Auphonic production API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        poll_interval: float = 5.0,
        timeout: float = 900.0,
    ) -> None:
        key = api_key or settings.auphonic_api_key
        if not key:
            raise AuphonicError(
                "Auphonic API key missing: set ATAMURAOKK_AUPHONIC_API_KEY.",
            )
        self._headers = {"Authorization": f"bearer {key}"}
        self._poll_interval = poll_interval
        self._timeout = timeout

    @staticmethod
    def _unwrap(resp: httpx.Response) -> dict[str, Any]:
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data")
        if not isinstance(data, dict):
            raise AuphonicError(f"unexpected Auphonic response: {body!r}")
        return data

    async def process(self, audio_path: Path, title: str, dest: Path) -> AuphonicResult:
        """Run ``audio_path`` through cleanup; write the result to ``dest``.

        Blocks (async) until the production is Done, then downloads the single
        MP3 output. Raises :class:`AuphonicError` on API/production failure or
        timeout.
        """
        async with httpx.AsyncClient(
            base_url=_BASE, headers=self._headers, timeout=60.0
        ) as http:
            uuid = await self._create(http, title)
            logger.info("Auphonic production {u} created for {t}", u=uuid, t=title)
            await self._upload(http, uuid, audio_path)
            await self._start(http, uuid)
            data = await self._await_done(http, uuid)
            url = self._output_url(data)
            await self._download(http, url, dest)
            return AuphonicResult(
                uuid=uuid,
                output_path=dest,
                status_string=str(data.get("status_string", "")),
            )

    async def _create(self, http: httpx.AsyncClient, title: str) -> str:
        data = self._unwrap(
            await http.post(
                "/productions.json",
                json={
                    "metadata": {"title": title},
                    "algorithms": _ALGORITHMS,
                    "output_files": _OUTPUT_FILES,
                },
            )
        )
        uuid = data.get("uuid")
        if not uuid:
            raise AuphonicError(f"no uuid in create response: {data!r}")
        return str(uuid)

    async def _upload(
        self, http: httpx.AsyncClient, uuid: str, audio_path: Path
    ) -> None:
        with audio_path.open("rb") as fh:
            self._unwrap(
                await http.post(
                    f"/production/{uuid}/upload.json",
                    files={"input_file": (audio_path.name, fh, "audio/mpeg")},
                    timeout=300.0,
                )
            )

    async def _start(self, http: httpx.AsyncClient, uuid: str) -> None:
        self._unwrap(await http.post(f"/production/{uuid}/start.json"))

    async def _await_done(self, http: httpx.AsyncClient, uuid: str) -> dict[str, Any]:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._timeout
        while True:
            data = self._unwrap(await http.get(f"/production/{uuid}.json"))
            status = data.get("status")
            if status == _STATUS_DONE:
                return data
            if status == _STATUS_ERROR:
                raise AuphonicError(
                    f"production {uuid} errored: "
                    f"{data.get('status_string')} / {data.get('error_message')}",
                )
            if loop.time() > deadline:
                raise AuphonicError(
                    f"production {uuid} timed out in state "
                    f"{data.get('status_string')!r}",
                )
            await asyncio.sleep(self._poll_interval)

    @staticmethod
    def _output_url(data: dict[str, Any]) -> str:
        files = data.get("output_files") or []
        for f in files:
            url = f.get("download_url")
            if url:
                return str(url)
        raise AuphonicError(f"no output download_url in production {data.get('uuid')}")

    async def _download(self, http: httpx.AsyncClient, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with http.stream(
            "GET", url, follow_redirects=True, timeout=300.0
        ) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes():
                    fh.write(chunk)
