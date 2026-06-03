"""faster-whisper implementation of :class:`Transcriber`.

Used by the Phase 0 spike on CPU/GPU locally; in production the same class
runs on a serverless GPU. ``faster-whisper`` is an optional dependency
(``uv sync --group spike``) so importing this module is deferred.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from AtamuraOKK.settings import settings
from AtamuraOKK.transcription.base import Segment, TranscriptResult

if TYPE_CHECKING:
    from faster_whisper import WhisperModel


class FasterWhisperTranscriber:
    """Transcribe with a faster-whisper model (default Whisper large-v3)."""

    def __init__(
        self,
        model_name: str | None = None,
        *,
        device: str | None = None,
        compute_type: str | None = None,
    ) -> None:
        self.model_name = model_name or settings.whisper_model
        self.device = device or settings.whisper_device
        self.compute_type = compute_type or settings.whisper_compute_type
        self._model: WhisperModel | None = None

    def _load(self) -> WhisperModel:
        if self._model is None:
            from faster_whisper import WhisperModel  # noqa: PLC0415

            logger.info(
                "Loading faster-whisper {model} (device={dev}, compute={ct})",
                model=self.model_name,
                dev=self.device,
                ct=self.compute_type,
            )
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
            )
        return self._model

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        speaker: str = "unknown",
    ) -> TranscriptResult:
        """Transcribe one mono audio file/channel (see :class:`Transcriber`)."""
        model = self._load()
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=language,
            vad_filter=True,
        )
        segments: list[Segment] = []
        texts: list[str] = []
        for seg in segments_iter:
            text = seg.text.strip()
            segments.append(
                Segment(
                    speaker=speaker,
                    start=round(seg.start, 2),
                    end=round(seg.end, 2),
                    text=text,
                ),
            )
            texts.append(text)

        return TranscriptResult(
            language=info.language,
            full_text=" ".join(texts).strip(),
            segments=segments,
            model=f"faster-whisper/{self.model_name}",
            meta={"language_probability": round(info.language_probability, 4)},
        )
