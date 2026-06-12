"""Transcribe downloaded meeting recordings — self-contained STT.

DOWNLOADED → TRANSCRIBED. Meetings are mono, so there is no agent/customer
channel to split (the LLM scorer infers roles); we downmix to a 16 kHz mono file
and run a single transcription. Engine is pluggable (Yandex SpeechKit v3 async
by default, or OpenAI gpt-4o-transcribe) and never touches the call pipeline's
transcription.
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from loguru import logger

from AtamuraOKK.scoring.meetings.config import config
from AtamuraOKK.scoring.meetings.media import to_mono_opus, to_mono_wav
from AtamuraOKK.scoring.meetings.store import MeetingStatus, MeetingStore


@dataclass(slots=True)
class TranscriptText:
    """A transcription result: the text plus the detected language."""

    text: str
    language: str


@runtime_checkable
class MeetingTranscriber(Protocol):
    """Turns a prepared 16 kHz mono recording into text + detected language."""

    async def transcribe(self, wav_path: Path) -> TranscriptText:
        """Transcribe one prepared audio file."""
        ...


class OpenAITranscriber:
    """OpenAI gpt-4o-transcribe (alternate engine; 25 MB / request limit)."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._api_key = api_key or config.openai_api_key
        self._model = model or config.openai_transcribe_model

    async def transcribe(self, wav_path: Path) -> TranscriptText:
        """Transcribe via the OpenAI audio API."""
        from openai import AsyncOpenAI  # noqa: PLC0415

        async with AsyncOpenAI(api_key=self._api_key) as client:
            with wav_path.open("rb") as fh:
                resp = await client.audio.transcriptions.create(
                    model=self._model,
                    file=fh,
                )
        return TranscriptText(text=resp.text.strip(), language="auto")


def build_transcriber() -> MeetingTranscriber:
    """Pick the transcriber from ``meetings_transcribe_engine``."""
    engine = config.meetings_transcribe_engine.lower()
    if engine == "openai":
        return OpenAITranscriber()
    if engine == "yandex":
        # Imported here, not at module top: yandex.py imports TranscriptText
        # back from this module.
        from AtamuraOKK.scoring.meetings.yandex import (  # noqa: PLC0415
            YandexTranscriber,
        )

        return YandexTranscriber()
    raise ValueError(f"unknown meetings_transcribe_engine: {engine!r}")


def _prepare_audio(audio_path: Path, workdir: Path, *, suffix: str = ".wav") -> Path:
    """Downmix to a 16 kHz mono file when ffmpeg is present; else the original.

    ``suffix`` comes from the engine (``.ogg`` → Opus for Yandex's 60 MB inline
    cap, ``.wav`` otherwise). Without ffmpeg we hand over the source as-is — the
    engine then rejects containers it can't take, failing just that recording.
    """
    convert = to_mono_opus if suffix == ".ogg" else to_mono_wav
    try:
        return convert(audio_path, workdir / f"mono{suffix}")
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
    concurrency: int | None = None,
) -> TranscribeStats:
    """Transcribe DOWNLOADED recordings into TRANSCRIBED, ``concurrency`` at a time.

    The STT engines are network-bound, so the batch fans out under a semaphore
    (``meetings_transcribe_concurrency`` by default). SQLite writes stay on the
    event loop — serialized, no cross-thread access to the store.
    """
    stats = TranscribeStats()
    limit = limit if limit is not None else config.meetings_batch_limit
    concurrency = concurrency or config.meetings_transcribe_concurrency
    own_store = store is None
    store = store or MeetingStore()
    transcriber = transcriber or build_transcriber()
    suffix = getattr(transcriber, "audio_suffix", ".wav")
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _one(row: sqlite3.Row) -> None:
        stats.attempted += 1
        file_id = int(row["file_id"])
        audio_path = row["audio_path"]
        try:
            async with semaphore:
                if not audio_path or not Path(audio_path).exists():
                    raise FileNotFoundError(f"audio missing: {audio_path}")
                with tempfile.TemporaryDirectory() as tmp:
                    src = await asyncio.to_thread(
                        _prepare_audio, Path(audio_path), Path(tmp), suffix=suffix
                    )
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

    try:
        rows = store.claim(MeetingStatus.DOWNLOADED, limit)
        logger.info(
            "Transcribing {n} meeting recordings ({c} concurrent)",
            n=len(rows),
            c=concurrency,
        )
        await asyncio.gather(*(_one(row) for row in rows))
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
