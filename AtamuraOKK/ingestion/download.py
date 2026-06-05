"""Download analyzable calls' recordings into object storage (NEW -> DOWNLOADED)."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from loguru import logger
from sqlalchemy import select

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.storage import get_storage

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


async def download_pending(*, limit: int = 200) -> DownloadStats:
    """Fetch recordings for analyzable NEW calls and store them in object storage."""
    stats = DownloadStats()
    storage = get_storage()
    await storage.ensure_bucket()

    async with (
        session_scope() as session,
        BitrixClient() as bx,
        httpx.AsyncClient(
            timeout=120.0,
            follow_redirects=True,
        ) as http,
    ):
        calls = (
            await session.scalars(
                select(Call)
                .where(Call.status == CallStatus.NEW, Call.analyzable.is_(True))
                .order_by(Call.started_at.asc())
                .limit(limit),
            )
        ).all()

        for call in calls:
            stats.attempted += 1
            call.attempts += 1
            try:
                url = await _resolve_url(call, bx)
                if not url:
                    raise ValueError("no recording url / file id")
                resp = await http.get(url)
                resp.raise_for_status()
                key = _object_key(call)
                await storage.put_bytes(
                    key,
                    resp.content,
                    content_type=_AUDIO_CONTENT_TYPE,
                )
                call.audio_object_key = key
                call.status = CallStatus.DOWNLOADED
                call.error = None
                stats.downloaded += 1
            except (BitrixError, httpx.HTTPError, ValueError, OSError) as exc:
                call.error = str(exc)
                if call.attempts >= _MAX_ATTEMPTS:
                    call.status = CallStatus.FAILED
                    stats.failed += 1
                logger.warning(
                    "Download failed for {id} (attempt {n}): {err}",
                    id=call.bitrix_call_id,
                    n=call.attempts,
                    err=exc,
                )
            # Commit per call so progress is durable and visible live.
            await session.commit()

    logger.info(
        "Download done: attempted={a} downloaded={d} failed={f}",
        a=stats.attempted,
        d=stats.downloaded,
        f=stats.failed,
    )
    return stats
