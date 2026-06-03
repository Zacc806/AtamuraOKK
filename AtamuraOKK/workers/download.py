"""Download job: fetch recordings for NEW calls -> DOWNLOADED."""

from __future__ import annotations

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.db.dao.call_dao import CallDAO
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.ingestion.recordings import (
    RecordingUnavailableError,
    download_recording,
)
from AtamuraOKK.settings import settings
from AtamuraOKK.transcription.channels import probe_channels
from AtamuraOKK.workers.context import safe_stem

_UNDATED = "undated"


async def run_download(factory: async_sessionmaker[AsyncSession]) -> int:
    """Download recordings for a batch of NEW calls. Returns count downloaded."""
    downloaded = 0
    async with (
        BitrixClient() as bx,
        httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http,
        factory() as session,
    ):
        calls = CallDAO(session)
        batch = await calls.claim_batch(CallStatus.NEW, settings.download_batch_size)
        for call in batch:
            date_part = (
                call.started_at.strftime("%Y/%m/%d") if call.started_at else _UNDATED
            )
            dest_dir = settings.audio_dir / date_part
            try:
                path = await download_recording(
                    bx,
                    http,
                    record_url=call.record_url,
                    record_file_id=call.record_file_id,
                    dest_dir=dest_dir,
                    stem=safe_stem(call.bitrix_call_id),
                )
            except RecordingUnavailableError as exc:
                await calls.mark(call, CallStatus.SKIPPED, error=str(exc))
                continue
            except (BitrixError, httpx.HTTPError) as exc:
                logger.warning("download failed for {id}: {e}", id=call.id, e=exc)
                await calls.mark(
                    call,
                    CallStatus.FAILED,
                    error=str(exc)[:500],
                    failed_stage="download",
                )
                continue

            call.audio_path = str(path)
            call.is_stereo = probe_channels(path) >= 2
            await calls.mark(call, CallStatus.DOWNLOADED)
            downloaded += 1
        await session.commit()
    logger.info("download: fetched {n} recordings", n=downloaded)
    return downloaded
