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


def _object_key(bitrix_call_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in bitrix_call_id)
    return f"calls/{safe}.mp3"


async def _fetch_recording(
    recording_url: str | None,
    record_file_id: int | None,
    bitrix_call_id: str,
    bx: BitrixClient,
    http: httpx.AsyncClient,
    storage: ObjectStorage,
) -> str:
    """Resolve the recording URL, download it, and store it in object storage.

    Holds **no** DB connection — runs between the claim-verify and result-commit
    transactions. Returns the object-storage key; raises on any failure so the
    caller records it against the call.
    """
    url = recording_url
    if not url and record_file_id:
        info = await bx.call("disk.file.get", {"id": record_file_id})
        if info:
            url = info.get("DOWNLOAD_URL")
    if not url:
        raise ValueError("no recording url / file id")
    resp = await http.get(url)
    resp.raise_for_status()
    key = _object_key(bitrix_call_id)
    await storage.put_bytes(key, resp.content, content_type=_AUDIO_CONTENT_TYPE)
    return key


def _apply_download(call: Call, key: str | None, error: str | None) -> str:
    """Settle a claimed call after a download attempt. Mutates ``call``; caller commits.

    On failure under the attempt cap the claim is released back to ``NEW`` so a
    later pass retries it; once the cap is hit the call is dead-lettered to
    ``FAILED``. Returns the resulting status value.
    """
    call.attempts += 1
    if error is None and key is not None:
        call.audio_object_key = key
        call.status = CallStatus.DOWNLOADED
        call.error = None
    else:
        call.error = error
        call.status = (
            CallStatus.FAILED if call.attempts >= _MAX_ATTEMPTS else CallStatus.NEW
        )
        logger.warning(
            "Download failed for {id} (attempt {n}): {err}",
            id=call.bitrix_call_id,
            n=call.attempts,
            err=error,
        )
    call.claimed_at = None
    return call.status.value


async def download_one(call_id: int) -> str:
    """Download one claimed (DOWNLOADING) call without holding a DB connection.

    Three short transactions bracket the slow transfer: verify the claim and read
    the source refs, release the connection for the Bitrix/HTTP/S3 round-trip,
    then reacquire to record the outcome (re-verifying the claim so a duplicate
    delivery returns ``"skipped"``). Returns the resulting status value.
    """
    storage = get_storage()
    await storage.ensure_bucket()

    async with session_scope() as session:
        call = await session.get(Call, call_id)
        if call is None or call.status != CallStatus.DOWNLOADING:
            return "skipped"
        recording_url = call.recording_url
        record_file_id = call.record_file_id
        bitrix_call_id = call.bitrix_call_id

    key: str | None = None
    error: str | None = None
    async with (
        BitrixClient() as bx,
        httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http,
    ):
        try:
            key = await _fetch_recording(
                recording_url, record_file_id, bitrix_call_id, bx, http, storage
            )
        except (BitrixError, httpx.HTTPError, ValueError, OSError) as exc:
            error = str(exc)

    async with session_scope() as session:
        call = await session.get(Call, call_id)
        if call is None or call.status != CallStatus.DOWNLOADING:
            return "skipped"
        return _apply_download(call, key, error)


async def download_pending(*, limit: int = 200) -> DownloadStats:
    """Claim and download analyzable NEW calls (single-process batch wrapper)."""
    stats = DownloadStats()
    call_ids = await claim_ready(CallStatus.NEW, CallStatus.DOWNLOADING, limit)
    if not call_ids:
        return stats

    for call_id in call_ids:
        status = await download_one(call_id)
        if status == "skipped":
            continue
        stats.attempted += 1
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
