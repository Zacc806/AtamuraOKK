"""Transcription worker: DOWNLOADED -> TRANSCRIBED (or PENDING_KK for Kazakh).

For each analyzable downloaded call: pull audio from object storage, split the
stereo channels (agent / customer), transcribe each with gpt-4o-transcribe, store
a speaker-labeled transcript, and detect the language. Kazakh calls are parked at
PENDING_KK (no Kazakh STT provider yet); Russian calls advance to TRANSCRIBED.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from loguru import logger
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.audio import extract_channel, probe_channels, to_mono_wav
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.dispatch.claim import claim_ready
from AtamuraOKK.settings import settings
from AtamuraOKK.storage import get_storage
from AtamuraOKK.storage.base import ObjectStorage
from AtamuraOKK.transcription.base import AsyncTranscriber, Segment, TranscriptResult
from AtamuraOKK.transcription.factory import get_transcriber
from AtamuraOKK.transcription.language import detect_language


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
    transcriber: AsyncTranscriber,
    audio_path: Path,
    workdir: Path,
) -> TranscriptResult:
    """Transcribe one recording into a speaker-labeled result."""
    # Some engines (Yandex async) take the whole multi-channel file in one
    # request and return per-channel results — skip the ffmpeg split for them.
    transcribe_file = getattr(transcriber, "transcribe_file", None)
    if getattr(transcriber, "wants_full_file", False) and transcribe_file:
        return await transcribe_file(audio_path)

    channels = probe_channels(audio_path)
    segments: list[Segment] = []
    model_label = ""
    if channels >= 2:
        for idx, speaker in ((0, "agent"), (1, "customer")):
            chan = extract_channel(audio_path, idx, workdir / f"ch{idx}.wav")
            res = await transcriber.transcribe_async(chan, speaker=speaker)
            segments.extend(res.segments)
            model_label = res.model
    else:
        mono = to_mono_wav(audio_path, workdir / "mono.wav")
        res = await transcriber.transcribe_async(mono, speaker="unknown")
        segments.extend(res.segments)
        model_label = res.model

    full_text = _blocks(segments)
    return TranscriptResult(
        language=detect_language(full_text),
        full_text=full_text,
        segments=segments,
        model=model_label,  # set by the provider, e.g. faster-whisper/large-v3
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


async def _transcribe_external(
    audio_object_key: str,
    transcriber: AsyncTranscriber,
    storage: ObjectStorage,
) -> TranscriptResult:
    """Download + transcribe one recording. Holds no DB connection.

    Runs between the claim-verify and result-commit transactions; raises on any
    failure so the caller records it against the call.
    """
    audio_bytes = await storage.download(audio_object_key)
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        src = tmpdir / Path(audio_object_key).name
        src.write_bytes(audio_bytes)
        return await _transcribe_audio(transcriber, src, tmpdir)


def _load_transcriber() -> AsyncTranscriber:
    """Build the configured transcriber and load its model (whisper) once."""
    transcriber = get_transcriber()
    load = getattr(transcriber, "load", None)
    if callable(load):
        load()
    return transcriber


async def transcribe_one(
    call_id: int,
    *,
    transcriber: AsyncTranscriber | None = None,
    storage: ObjectStorage | None = None,
) -> str:
    """Transcribe one claimed (TRANSCRIBING) call in its own session.

    The unit of work for the broker task. Pass a pre-loaded ``transcriber`` to
    reuse the model across calls; otherwise it is built and loaded here. Returns
    the resulting status value, or ``"skipped"`` if no longer claimed.
    """
    if transcriber is None:
        transcriber = await asyncio.to_thread(_load_transcriber)
    storage = storage or get_storage()

    async with session_scope() as session:
        call = await session.get(Call, call_id)
        if call is None or call.status != CallStatus.TRANSCRIBING:
            return "skipped"
        audio_object_key = call.audio_object_key
        bx_id = call.bitrix_call_id

    if not audio_object_key:
        async with session_scope() as session:
            call = await session.get(Call, call_id)
            if call is None or call.status != CallStatus.TRANSCRIBING:
                return "skipped"
            call.status = CallStatus.FAILED
            call.error = "no audio_object_key"
            call.claimed_at = None
            return call.status.value

    result: TranscriptResult | None = None
    error: str | None = None
    try:
        result = await _transcribe_external(audio_object_key, transcriber, storage)
    except Exception as exc:  # record + move on
        error = f"transcription: {exc}"
        logger.warning("Transcription failed for {id}: {e}", id=bx_id, e=exc)

    async with session_scope() as session:
        call = await session.get(Call, call_id)
        if call is None or call.status != CallStatus.TRANSCRIBING:
            return "skipped"
        if result is not None:
            call.language = result.language
            handles_kazakh = getattr(transcriber, "handles_kazakh", False)
            if result.language == "kk" and not handles_kazakh:
                # Park Kazakh when the engine can't handle it (whisper/openai).
                call.status = CallStatus.PENDING_KK
                call.error = None
            else:
                await _persist_transcript(session, call.id, result)
                call.status = CallStatus.TRANSCRIBED
                call.error = None
        else:
            call.attempts += 1
            call.status = CallStatus.FAILED
            call.error = error
        call.claimed_at = None
        return call.status.value


def _tally(stats: TranscribeStats, status: str) -> None:
    stats.attempted += 1
    if status == CallStatus.TRANSCRIBED.value:
        stats.transcribed += 1
    elif status == CallStatus.PENDING_KK.value:
        stats.pending_kk += 1
    elif status == CallStatus.FAILED.value:
        stats.failed += 1


async def requeue_pending_kk(*, limit: int | None = None) -> int:
    """Revert parked Kazakh calls (PENDING_KK -> DOWNLOADED) for re-transcription.

    Used after switching to a Kazakh-capable engine (SpeechKit): the next
    transcription pass re-claims them. They still hold their ``audio_object_key``
    from the original download, so nothing needs re-fetching. Returns the count
    requeued.
    """
    from sqlalchemy import select, update  # noqa: PLC0415

    async with session_scope() as session:
        stmt = select(Call.id).where(Call.status == CallStatus.PENDING_KK)
        if limit is not None:
            stmt = stmt.limit(limit)
        ids = list((await session.execute(stmt)).scalars().all())
        if not ids:
            logger.info("No PENDING_KK calls to requeue.")
            return 0
        await session.execute(
            update(Call)
            .where(Call.id.in_(ids))
            .values(status=CallStatus.DOWNLOADED, error=None, claimed_at=None),
        )
    logger.info("Requeued {n} PENDING_KK call(s) -> DOWNLOADED", n=len(ids))
    return len(ids)


async def transcribe_pending(
    *,
    limit: int = 50,
    concurrency: int | None = None,
) -> TranscribeStats:
    """Claim and transcribe analyzable DOWNLOADED calls concurrently."""
    concurrency = concurrency or settings.transcribe_concurrency
    stats = TranscribeStats()
    storage = get_storage()

    # Pre-load the model once (whisper) so concurrent tasks don't race the load.
    transcriber = await asyncio.to_thread(_load_transcriber)

    call_ids = await claim_ready(
        CallStatus.DOWNLOADED,
        CallStatus.TRANSCRIBING,
        limit,
    )
    if not call_ids:
        return stats

    progress = {"done": 0, "total": len(call_ids)}
    sem = asyncio.Semaphore(concurrency)
    logger.info(
        "Transcribing {n} calls (concurrency={c})", n=len(call_ids), c=concurrency
    )

    async def run(cid: int) -> None:
        async with sem:
            status = await transcribe_one(cid, transcriber=transcriber, storage=storage)
        if status != "skipped":
            _tally(stats, status)
        progress["done"] += 1
        logger.info(
            "Transcribed {n}/{total}: call {id} -> {st}",
            n=progress["done"],
            total=progress["total"],
            id=cid,
            st=status,
        )

    results = await asyncio.gather(
        *(run(cid) for cid in call_ids), return_exceptions=True
    )
    for cid, res in zip(call_ids, results, strict=True):
        if isinstance(res, Exception):
            logger.error("Transcription task crashed for call {id}: {e}", id=cid, e=res)

    logger.info(
        "Transcription done: attempted={a} transcribed={t} pending_kk={k} failed={f}",
        a=stats.attempted,
        t=stats.transcribed,
        k=stats.pending_kk,
        f=stats.failed,
    )
    return stats
