"""Transcription worker CLI: DOWNLOADED -> TRANSCRIBED.

``python -m AtamuraOKK.transcription`` — pull each downloaded recording from
object storage, transcribe it (Groq Whisper for Russian, Yandex SpeechKit for
Kazakh), and persist the transcript.
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from dataclasses import asdict
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.storage import get_storage
from AtamuraOKK.transcription.pipeline import transcribe_file
from AtamuraOKK.transcription.router import build_transcriber


async def transcribe_pending(*, limit: int = 50) -> int:
    """Transcribe a batch of DOWNLOADED calls. Returns count transcribed."""
    transcriber = build_transcriber()
    storage = get_storage()
    done = 0
    async with session_scope() as session:
        calls = (
            await session.scalars(
                select(Call)
                .where(Call.status == CallStatus.DOWNLOADED)
                .order_by(Call.started_at.asc())
                .limit(limit),
            )
        ).all()
        for call in calls:
            if not call.audio_object_key:
                call.status = CallStatus.FAILED
                call.error = "no audio_object_key"
                continue
            try:
                audio = await storage.download(call.audio_object_key)
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "audio.mp3"
                    path.write_bytes(audio)
                    result = await asyncio.to_thread(
                        transcribe_file,
                        transcriber,
                        path,
                        is_stereo=bool(call.is_stereo),
                    )
            except Exception as exc:  # worker boundary: log + mark FAILED
                logger.warning("transcribe failed for {id}: {e}", id=call.id, e=exc)
                call.status = CallStatus.FAILED
                call.error = str(exc)[:500]
                continue

            session.add(
                Transcript(
                    call_id=call.id,
                    language=result.language,
                    full_text=result.full_text,
                    segments=[asdict(s) for s in result.segments],
                    model=result.model,
                ),
            )
            call.language = result.language
            call.status = CallStatus.TRANSCRIBED
            done += 1
    logger.info("transcribe: completed {n} calls", n=done)
    return done


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(prog="python -m AtamuraOKK.transcription")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    asyncio.run(transcribe_pending(limit=args.limit))


if __name__ == "__main__":
    main()
