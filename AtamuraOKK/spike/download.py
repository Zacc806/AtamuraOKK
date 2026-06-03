"""Stage 2: download each sampled call's recording to local disk.

The primary path is the direct one: ``voximplant.statistic.get`` returns a
``CALL_RECORD_URL`` for each recorded call — a direct link to the mp3 in the
Voximplant cloud. We stream that straight to disk; no Bitrix **Disk** scope is
involved.

As a fallback for portals whose telephony stores recordings as Bitrix Drive
files (``CALL_RECORD_URL`` empty, ``RECORD_FILE_ID`` set), we resolve the file
id via ``disk.file.get`` (needs the ``disk`` scope) and download its
``DOWNLOAD_URL``.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.settings import settings

# How many recordings to fetch at once.
_MAX_CONCURRENCY = 6
# Transient HTTP failures worth retrying (network blips, cloud 5xx).
_DOWNLOAD_RETRIES = 3
# Map common audio content-types to a file extension when the URL has none.
_CONTENT_TYPE_EXT = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
}


def _audio_dir() -> Path:
    d = settings.spike_dir / "audio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(call_id: str) -> str:
    """Make a Bitrix CALL_ID safe for use as a filename."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in call_id)


def _ext_from_url(url: str) -> str:
    """File extension embedded in the URL path, or '' if none."""
    name = url.split("?", 1)[0].rsplit("/", 1)[-1]
    return Path(name).suffix


def _ext_from_content_type(content_type: str | None) -> str:
    """Best-effort extension for a response Content-Type (defaults to .mp3)."""
    if not content_type:
        return ".mp3"
    base = content_type.split(";", 1)[0].strip().lower()
    return _CONTENT_TYPE_EXT.get(base) or mimetypes.guess_extension(base) or ".mp3"


async def _resolve_disk_url(bx: BitrixClient, file_id: str) -> str | None:
    """Resolve a Bitrix Drive file id to a direct download URL (fallback path)."""
    info = await bx.call("disk.file.get", {"id": file_id})
    if not info:
        return None
    # DOWNLOAD_URL is a relative-or-absolute link with an embedded auth token.
    return info.get("DOWNLOAD_URL")


def _existing_audio(call_id: str) -> Path | None:
    """Return an already-downloaded recording for this call, if any."""
    prefix = _safe(call_id)
    for path in _audio_dir().glob(f"{prefix}.*"):
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


async def _stream_to_disk(http: httpx.AsyncClient, url: str, call_id: str) -> Path:
    """Stream ``url`` to ``<audio_dir>/<call_id><ext>`` and return the path.

    Extension is taken from the URL path, else inferred from Content-Type
    (Voximplant cloud URLs are typically extension-less). Retries transient
    failures with exponential backoff.
    """
    delay = settings.bitrix_retry_base_delay
    last_exc: httpx.HTTPError | None = None
    for attempt in range(1, _DOWNLOAD_RETRIES + 1):
        try:
            async with http.stream("GET", url) as resp:
                resp.raise_for_status()
                ext = _ext_from_url(url) or _ext_from_content_type(
                    resp.headers.get("content-type"),
                )
                dest = _audio_dir() / f"{_safe(call_id)}{ext}"
                with dest.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=1 << 16):
                        fh.write(chunk)
                return dest
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < _DOWNLOAD_RETRIES:
                logger.warning(
                    "Download {id} failed ({err}); retry {n} in {d}s",
                    id=call_id,
                    err=exc,
                    n=attempt,
                    d=delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
    raise last_exc if last_exc else httpx.HTTPError("download failed")


async def _download_one(
    call: dict[str, Any],
    bx: BitrixClient,
    http: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> bool:
    """Download one call's recording; annotate the record in place."""
    call_id = call["CALL_ID"]
    call.pop("download_error", None)

    existing = _existing_audio(call_id)
    if existing is not None:
        call["audio_path"] = str(existing)
        return True

    async with sem:
        try:
            # Primary: direct Voximplant cloud mp3 link.
            url = call.get("CALL_RECORD_URL")
            # Fallback: resolve a Bitrix Drive file id via the disk scope.
            if not url and call.get("RECORD_FILE_ID"):
                url = await _resolve_disk_url(bx, str(call["RECORD_FILE_ID"]))
            if not url:
                call["download_error"] = "no record url / file id"
                return False

            dest = await _stream_to_disk(http, url, call_id)
            call["audio_path"] = str(dest)
        except (BitrixError, httpx.HTTPError, OSError) as exc:
            call["download_error"] = str(exc)
            logger.warning("Download failed for {id}: {err}", id=call_id, err=exc)
            return False
        else:
            return True


async def download_all() -> list[dict[str, Any]]:
    """Download recordings for every call in ``calls.json``.

    Idempotent: calls with a recording already on disk are skipped. Returns the
    call records annotated with ``audio_path`` (or ``download_error``).
    """
    calls_path = settings.spike_dir / "calls.json"
    if not calls_path.exists():
        raise FileNotFoundError(
            f"{calls_path} not found — run `python -m AtamuraOKK.spike fetch` first.",
        )
    calls: list[dict[str, Any]] = json.loads(calls_path.read_text(encoding="utf-8"))
    _audio_dir()

    sem = asyncio.Semaphore(_MAX_CONCURRENCY)
    async with (
        BitrixClient() as bx,
        httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http,
    ):
        results = await asyncio.gather(
            *(_download_one(call, bx, http, sem) for call in calls),
        )
    ok = sum(results)

    calls_path.write_text(
        json.dumps(calls, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Downloaded {ok}/{total} recordings.", ok=ok, total=len(calls))

    scope_blocked = sum(
        1 for c in calls if "INSUFFICIENT_SCOPE" in (c.get("download_error") or "")
    )
    if scope_blocked:
        logger.warning(
            "{n} calls had no CALL_RECORD_URL and their Bitrix Drive recording "
            "needs the 'disk' scope. Add 'disk' to the inbound webhook's "
            "permissions, then re-run download to fetch them.",
            n=scope_blocked,
        )
    return calls
