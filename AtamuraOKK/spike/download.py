"""Stage 2: download each sampled call's recording to local disk.

Recordings on this portal are Bitrix Drive files referenced by RECORD_FILE_ID
(CALL_RECORD_URL is empty). Resolving them needs the **disk** scope on the
webhook. ``disk.file.get`` returns a ``DOWNLOAD_URL`` carrying its own auth
token, which we then GET directly.

If a future portal uses native Voximplant telephony, CALL_RECORD_URL will be
populated and is downloaded directly — handled here as a fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.settings import settings


def _audio_dir() -> Path:
    d = settings.spike_dir / "audio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ext_from_url(url: str, default: str = ".mp3") -> str:
    name = url.split("?", 1)[0].rsplit("/", 1)[-1]
    return Path(name).suffix or default


async def _resolve_download_url(bx: BitrixClient, file_id: str) -> str | None:
    """Resolve a Bitrix Drive file id to a direct download URL."""
    info = await bx.call("disk.file.get", {"id": file_id})
    if not info:
        return None
    # DOWNLOAD_URL is a relative-or-absolute link with an embedded auth token.
    return info.get("DOWNLOAD_URL")


async def download_all() -> list[dict[str, Any]]:
    """Download recordings for every call in ``calls.json``.

    Returns the call records annotated with ``audio_path`` (or ``download_error``).
    """
    calls_path = settings.spike_dir / "calls.json"
    if not calls_path.exists():
        raise FileNotFoundError(
            f"{calls_path} not found — run `python -m AtamuraOKK.spike fetch` first.",
        )
    calls: list[dict[str, Any]] = json.loads(calls_path.read_text(encoding="utf-8"))
    audio_dir = _audio_dir()

    ok = 0
    async with (
        BitrixClient() as bx,
        httpx.AsyncClient(
            timeout=120.0,
            follow_redirects=True,
        ) as http,
    ):
        for call in calls:
            call_id = call["CALL_ID"]
            try:
                url = call.get("CALL_RECORD_URL")
                if not url and call.get("RECORD_FILE_ID"):
                    url = await _resolve_download_url(bx, str(call["RECORD_FILE_ID"]))
                if not url:
                    call["download_error"] = "no record url / file id"
                    continue

                resp = await http.get(url)
                resp.raise_for_status()
                dest = audio_dir / f"{_safe(call_id)}{_ext_from_url(url)}"
                dest.write_bytes(resp.content)
                call["audio_path"] = str(dest)
                ok += 1
            except (BitrixError, httpx.HTTPError) as exc:
                call["download_error"] = str(exc)
                logger.warning("Download failed for {id}: {err}", id=call_id, err=exc)

    calls_path.write_text(
        json.dumps(calls, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Downloaded {ok}/{total} recordings.", ok=ok, total=len(calls))
    return calls


def _safe(call_id: str) -> str:
    """Make a Bitrix CALL_ID safe for use as a filename."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in call_id)
