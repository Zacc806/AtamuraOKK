"""Transcription worker: DOWNLOADED -> TRANSCRIBED (or PENDING_KK for Kazakh).

For each analyzable downloaded call: pull audio from object storage, split the
stereo channels (agent / customer), transcribe each with gpt-4o-transcribe, store
a speaker-labeled transcript, and detect the language. Kazakh calls are parked at
PENDING_KK (no Kazakh STT provider yet); Russian calls advance to TRANSCRIBED.
"""

from __future__ import annotations

import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.audio import extract_channel, probe_channels, to_mono_wav
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.storage import get_storage
from AtamuraOKK.transcription.base import Segment, TranscriptResult
from AtamuraOKK.transcription.language import detect_language
from AtamuraOKK.transcription.openai_provider import OpenAITranscriber


@dataclass
class TranscribeStats:
    """Summary of one transcription pass."""

    attempted: int = 0
    transcribed: int = 0
    pending_kk: int = 0
    failed: int = 0


def _blocks(segments: list[Segment]) -> str:
    """Render speaker segments as labeled blocks (no timestamps available)."""
    parts = [f"[{s.speaker.upper()}]\n{s.text}".strip() for s in segments if s.text]
    return "\n\n".join(parts)


async def _transcribe_audio(
    transcriber: OpenAITranscriber,
    audio_path: Path,
    workdir: Path,
) -> TranscriptResult:
    """Transcribe one recording into a speaker-labeled result."""
    channels = probe_channels(audio_path)
    segments: list[Segment] = []
    if channels >= 2:
        for idx, speaker in ((0, "agent"), (1, "customer")):
            chan = extract_channel(audio_path, idx, workdir / f"ch{idx}.wav")
            res = await transcriber.transcribe_async(chan, speaker=speaker)
            segments.extend(res.segments)
    else:
        mono = to_mono_wav(audio_path, workdir / "mono.wav")
        res = await transcriber.transcribe_async(mono, speaker="unknown")
        segments.extend(res.segments)

    full_text = _blocks(segments)
    return TranscriptResult(
        language=detect_language(full_text),
        full_text=full_text,
        segments=segments,
        model=f"openai/{transcriber.model}",
        meta={"channels": channels, "stereo": channels >= 2},
    )


async def _persist_transcript(
    session: AsyncSession,
    call_id: int,
    result: TranscriptResult,
) -> None:
    """Upsert the transcript row for a call (one transcript per call)."""
    values = {
        "call_id": call_id,
        "language": result.language,
        "full_text": result.full_text,
        "segments": [asdict(s) for s in result.segments],
        "model": result.model,
    }
    stmt = insert(Transcript).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["call_id"],
        set_={
            k: stmt.excluded[k] for k in ("language", "full_text", "segments", "model")
        },
    )
    await session.execute(stmt)


async def transcribe_pending(*, limit: int = 50) -> TranscribeStats:
    """Transcribe analyzable DOWNLOADED calls; Kazakh -> PENDING_KK."""
    stats = TranscribeStats()
    storage = get_storage()
    transcriber = OpenAITranscriber()

    async with session_scope() as session:
        calls = (
            await session.scalars(
                select(Call)
                .where(Call.status == CallStatus.DOWNLOADED, Call.analyzable.is_(True))
                .order_by(Call.started_at.asc())
                .limit(limit),
            )
        ).all()

        for call in calls:
            stats.attempted += 1
            if not call.audio_object_key:
                call.status = CallStatus.FAILED
                call.error = "no audio_object_key"
                stats.failed += 1
                continue
            try:
                audio_bytes = await storage.download(call.audio_object_key)
                with tempfile.TemporaryDirectory() as tmp:
                    tmpdir = Path(tmp)
                    src = tmpdir / Path(call.audio_object_key).name
                    src.write_bytes(audio_bytes)
                    result = await _transcribe_audio(transcriber, src, tmpdir)

                call.language = result.language
                if result.language == "kk":
                    # Park: no Kazakh STT provider yet. Don't store the unreliable
                    # gpt-4o-transcribe output; re-transcribe when one exists.
                    call.status = CallStatus.PENDING_KK
                    call.error = None
                    stats.pending_kk += 1
                else:
                    await _persist_transcript(session, call.id, result)
                    call.status = CallStatus.TRANSCRIBED
                    call.error = None
                    stats.transcribed += 1
            except Exception as exc:  # record + continue to next call
                call.attempts += 1
                call.status = CallStatus.FAILED
                call.error = f"transcription: {exc}"
                stats.failed += 1
                logger.warning(
                    "Transcription failed for {id}: {e}",
                    id=call.bitrix_call_id,
                    e=exc,
                )

    logger.info(
        "Transcription done: attempted={a} transcribed={t} pending_kk={k} failed={f}",
        a=stats.attempted,
        t=stats.transcribed,
        k=stats.pending_kk,
        f=stats.failed,
    )
    return stats
