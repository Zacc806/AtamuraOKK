"""Download analyzable calls' recordings into object storage (NEW -> DOWNLOADED).

Work is claimed race-safely (NEW -> DOWNLOADING) via :mod:`AtamuraOKK.dispatch.claim`
so multiple download workers never fetch the same recording twice.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from loguru import logger

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.dispatch.claim import claim_ready
from AtamuraOKK.storage import get_storage
from AtamuraOKK.storage.base import ObjectStorage

# Give up (-> FAILED) after this many download attempts.
_MAX_ATTEMPTS = 4
_AUDIO_CONTENT_TYPE = "audio/mpeg"


@dataclass
class DownloadStats:
    """Summary of one download pass."""

    attempted: int = 0
    downloaded: int = 0
    failed: int = 0


def _object_key(call: Call) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in call.bitrix_call_id)
    return f"calls/{safe}.mp3"


async def _resolve_url(call: Call, bx: BitrixClient) -> str | None:
    if call.recording_url:
        return call.recording_url
    if call.record_file_id:
        info = await bx.call("disk.file.get", {"id": call.record_file_id})
        if info:
            return info.get("DOWNLOAD_URL")
    return None


async def _download_call(
    call: Call,
    bx: BitrixClient,
    http: httpx.AsyncClient,
    storage: ObjectStorage,
) -> str:
    """Download one already-claimed call. Mutates ``call``; caller commits.

    On failure under the attempt cap the claim is released back to ``NEW`` so a
    later pass retries it; once the cap is hit the call is dead-lettered to
    ``FAILED``. Returns the resulting status value.
    """
    call.attempts += 1
    try:
        url = await _resolve_url(call, bx)
        if not url:
            raise ValueError("no recording url / file id")
        resp = await http.get(url)
        resp.raise_for_status()
        key = _object_key(call)
        await storage.put_bytes(key, resp.content, content_type=_AUDIO_CONTENT_TYPE)
        call.audio_object_key = key
        call.status = CallStatus.DOWNLOADED
        call.error = None
    except (BitrixError, httpx.HTTPError, ValueError, OSError) as exc:
        call.error = str(exc)
        call.status = (
            CallStatus.FAILED if call.attempts >= _MAX_ATTEMPTS else CallStatus.NEW
        )
        logger.warning(
            "Download failed for {id} (attempt {n}): {err}",
            id=call.bitrix_call_id,
            n=call.attempts,
            err=exc,
        )
    call.claimed_at = None
    return call.status.value


async def download_one(call_id: int) -> str:
    """Download one claimed (DOWNLOADING) call in its own session/clients.

    The unit of work for the broker task. Returns the resulting status value, or
    ``"skipped"`` if the row is no longer claimed for download.
    """
    storage = get_storage()
    await storage.ensure_bucket()
    async with (
        session_scope() as session,
        BitrixClient() as bx,
        httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http,
    ):
        call = await session.get(Call, call_id)
        if call is None or call.status != CallStatus.DOWNLOADING:
            return "skipped"
        return await _download_call(call, bx, http, storage)


async def download_pending(*, limit: int = 200) -> DownloadStats:
    """Claim and download analyzable NEW calls (single-process batch wrapper)."""
    stats = DownloadStats()
    call_ids = await claim_ready(CallStatus.NEW, CallStatus.DOWNLOADING, limit)
    if not call_ids:
        return stats

    storage = get_storage()
    await storage.ensure_bucket()
    async with (
        BitrixClient() as bx,
        httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http,
    ):
        for call_id in call_ids:
            async with session_scope() as session:
                call = await session.get(Call, call_id)
                if call is None or call.status != CallStatus.DOWNLOADING:
                    continue
                stats.attempted += 1
                status = await _download_call(call, bx, http, storage)
            if status == CallStatus.DOWNLOADED.value:
                stats.downloaded += 1
            elif status == CallStatus.FAILED.value:
                stats.failed += 1

    logger.info(
        "Download done: attempted={a} downloaded={d} failed={f}",
        a=stats.attempted,
        d=stats.downloaded,
        f=stats.failed,
    )
    return stats
