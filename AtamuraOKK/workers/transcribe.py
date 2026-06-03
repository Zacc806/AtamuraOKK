"""Transcription job: DOWNLOADED -> TRANSCRIBED via Groq Whisper."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from AtamuraOKK.db.dao.call_dao import CallDAO
from AtamuraOKK.db.dao.transcript_dao import TranscriptDAO
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.settings import settings
from AtamuraOKK.transcription.base import Transcriber
from AtamuraOKK.transcription.pipeline import transcribe_file


async def run_transcribe(
    factory: async_sessionmaker[AsyncSession],
    transcriber: Transcriber,
) -> int:
    """Transcribe a batch of DOWNLOADED calls. Returns count transcribed."""
    done = 0
    async with factory() as session:
        calls = CallDAO(session)
        transcripts = TranscriptDAO(session)
        batch = await calls.claim_batch(
            CallStatus.DOWNLOADED,
            settings.transcribe_batch_size,
        )
        for call in batch:
            if not call.audio_path:
                await calls.mark(
                    call,
                    CallStatus.FAILED,
                    error="no audio_path",
                    failed_stage="transcribe",
                )
                continue
            try:
                result = await asyncio.to_thread(
                    transcribe_file,
                    transcriber,
                    Path(call.audio_path),
                    is_stereo=bool(call.is_stereo),
                )
            except Exception as exc:  # worker boundary: log + mark failed
                logger.warning("transcribe failed for {id}: {e}", id=call.id, e=exc)
                await calls.mark(
                    call,
                    CallStatus.FAILED,
                    error=str(exc)[:500],
                    failed_stage="transcribe",
                )
                continue

            await transcripts.create(
                call_id=call.id,
                language=result.language,
                full_text=result.full_text,
                segments=[asdict(s) for s in result.segments],
                model=result.model,
                language_probability=result.meta.get("language_probability"),
                meta=result.meta,
            )
            await calls.mark(call, CallStatus.TRANSCRIBED)
            done += 1
        await session.commit()
    logger.info("transcribe: completed {n} calls", n=done)
    return done
