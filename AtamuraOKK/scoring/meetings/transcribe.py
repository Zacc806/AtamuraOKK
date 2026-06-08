"""Transcribe downloaded meeting recordings — self-contained STT.

DOWNLOADED → TRANSCRIBED. Meetings are mono, so there is no agent/customer
channel to split (the LLM scorer infers roles); we downmix to 16 kHz WAV and run
a single transcription. Engine is pluggable (local faster-whisper by default, or
OpenAI gpt-4o-transcribe) and never touches the call pipeline's transcription.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from AtamuraOKK.scoring.meetings.config import config
from AtamuraOKK.scoring.meetings.media import to_mono_wav
from AtamuraOKK.scoring.meetings.store import MeetingStatus, MeetingStore


@dataclass(slots=True)
class TranscriptText:
    """A transcription result: the text plus the detected language."""

    text: str
    language: str


@runtime_checkable
class MeetingTranscriber(Protocol):
    """Turns a 16 kHz mono WAV into text + detected language."""

    async def transcribe(self, wav_path: Path) -> TranscriptText:
        """Transcribe one prepared WAV file."""
        ...


class WhisperTranscriber:
    """Local faster-whisper transcriber (multilingual: ru + kk, no API quota)."""

    def __init__(
        self,
        model: str | None = None,
        *,
        device: str | None = None,
        compute_type: str | None = None,
    ) -> None:
        self._model_name = model or config.meetings_whisper_model
        self._device = device or config.meetings_whisper_device
        self._compute_type = compute_type or config.meetings_whisper_compute_type
        self._model: Any = None

    def load(self) -> None:
        """Load the model once (blocking); safe to call repeatedly."""
        if self._model is not None:
            return
        from faster_whisper import WhisperModel  # noqa: PLC0415

        self._model = WhisperModel(
            self._model_name,
            device=self._device,
            compute_type=self._compute_type,
        )

    def _transcribe_sync(self, wav_path: Path) -> TranscriptText:
        self.load()
        assert self._model is not None  # noqa: S101 (loaded above)
        segments, info = self._model.transcribe(str(wav_path), vad_filter=True)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return TranscriptText(text=text, language=str(info.language or "auto"))

    async def transcribe(self, wav_path: Path) -> TranscriptText:
        """Transcribe off the event loop (CTranslate2 is blocking)."""
        return await asyncio.to_thread(self._transcribe_sync, wav_path)


class OpenAITranscriber:
    """OpenAI gpt-4o-transcribe (alternate engine; 25 MB / request limit)."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._api_key = api_key or config.openai_api_key
        self._model = model or config.openai_transcribe_model

    async def transcribe(self, wav_path: Path) -> TranscriptText:
        """Transcribe via the OpenAI audio API."""
        from openai import AsyncOpenAI  # noqa: PLC0415

        client = AsyncOpenAI(api_key=self._api_key)
        with wav_path.open("rb") as fh:
            resp = await client.audio.transcriptions.create(model=self._model, file=fh)
        return TranscriptText(text=resp.text.strip(), language="auto")


def build_transcriber() -> MeetingTranscriber:
    """Pick the transcriber from ``meetings_transcribe_engine``."""
    engine = config.meetings_transcribe_engine.lower()
    if engine == "openai":
        return OpenAITranscriber()
    return WhisperTranscriber()


def _prepare_audio(audio_path: Path, workdir: Path) -> Path:
    """Downmix to a mono WAV when ffmpeg is present; else use the original.

    faster-whisper decodes any container itself (PyAV), so the ffmpeg downmix is
    an optimization (smaller input, helps the OpenAI 25 MB cap) — not a hard
    requirement. If the ffmpeg binary is missing we transcribe the source as-is.
    """
    try:
        return to_mono_wav(audio_path, workdir / "mono.wav")
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("ffmpeg unavailable ({e}); transcribing original audio", e=exc)
        return audio_path


@dataclass
class TranscribeStats:
    """Summary of one transcription pass."""

    attempted: int = 0
    transcribed: int = 0
    failed: int = 0


async def transcribe_pending(
    *,
    limit: int | None = None,
    store: MeetingStore | None = None,
    transcriber: MeetingTranscriber | None = None,
) -> TranscribeStats:
    """Transcribe DOWNLOADED recordings into TRANSCRIBED."""
    stats = TranscribeStats()
    limit = limit if limit is not None else config.meetings_batch_limit
    own_store = store is None
    store = store or MeetingStore()
    transcriber = transcriber or build_transcriber()

    load = getattr(transcriber, "load", None)
    if callable(load):
        await asyncio.to_thread(load)

    try:
        rows = store.claim(MeetingStatus.DOWNLOADED, limit)
        logger.info("Transcribing {n} meeting recordings", n=len(rows))
        for row in rows:
            stats.attempted += 1
            file_id = int(row["file_id"])
            audio_path = row["audio_path"]
            try:
                if not audio_path or not Path(audio_path).exists():
                    raise FileNotFoundError(f"audio missing: {audio_path}")
                with tempfile.TemporaryDirectory() as tmp:
                    src = _prepare_audio(Path(audio_path), Path(tmp))
                    result = await transcriber.transcribe(src)
                if not result.text.strip():
                    raise ValueError("empty transcript")
                store.mark_transcribed(file_id, result.text, result.language)
                stats.transcribed += 1
            except Exception as exc:
                dead = store.bump_attempt(
                    file_id,
                    f"transcribe: {exc}",
                    max_attempts=config.meetings_max_attempts,
                )
                stats.failed += int(dead)
                logger.warning(
                    "Meeting transcription failed for {id}: {e}", id=file_id, e=exc
                )
    finally:
        if own_store:
            store.close()

    logger.info(
        "Meeting transcription: attempted={a} transcribed={t} failed={f}",
        a=stats.attempted,
        t=stats.transcribed,
        f=stats.failed,
    )
    return stats
