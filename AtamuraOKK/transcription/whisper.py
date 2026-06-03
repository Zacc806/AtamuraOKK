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

    def _build(self, *, local_files_only: bool) -> WhisperModel:
        from faster_whisper import WhisperModel  # noqa: PLC0415

        return WhisperModel(
            self.model_name,
            device=self.device,
            compute_type=self.compute_type,
            local_files_only=local_files_only,
        )

    def _load(self) -> WhisperModel:
        if self._model is None:
            logger.info(
                "Loading faster-whisper {model} (device={dev}, compute={ct})...",
                model=self.model_name,
                dev=self.device,
                ct=self.compute_type,
            )
            # Prefer the local cache: avoids a per-run HF Hub round-trip that, on a
            # slow connection, makes load look "stuck". Fall back to downloading
            # only when the model isn't cached yet.
            try:
                self._model = self._build(local_files_only=True)
            except Exception:  # any cache miss -> download once
                logger.info("Model not cached; downloading from HF Hub (one-time)...")
                self._model = self._build(local_files_only=False)
            logger.info("Model ready.")
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
            # Anti-hallucination: on a near-silent channel (common with
            # dual-channel telephony, where one party is quiet) Whisper invents
            # phrases like "Продолжение следует...". VAD skips silence; disabling
            # cross-segment conditioning stops runaway repetition; the silence
            # threshold drops invented spans during long pauses.
            vad_filter=True,
            condition_on_previous_text=False,
            hallucination_silence_threshold=2.0,
        )
        logger.info(
            "  [{speaker}] {dur:.0f}s audio, lang={lang} ({prob:.2f}) - decoding...",
            speaker=speaker,
            dur=info.duration,
            lang=info.language,
            prob=info.language_probability,
        )

        # Segments stream lazily — decoding happens as we iterate. Log progress
        # against the known duration so a long channel isn't a silent black box.
        segments: list[Segment] = []
        texts: list[str] = []
        next_mark = 30.0
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
            if seg.end >= next_mark:
                pct = (
                    min(100.0, 100.0 * seg.end / info.duration) if info.duration else 0
                )
                logger.info(
                    "    [{speaker}] {pct:.0f}% ({end:.0f}/{dur:.0f}s)",
                    speaker=speaker,
                    pct=pct,
                    end=seg.end,
                    dur=info.duration,
                )
                next_mark += 30.0

        return TranscriptResult(
            language=info.language,
            full_text=" ".join(texts).strip(),
            segments=segments,
            model=f"faster-whisper/{self.model_name}",
            meta={"language_probability": round(info.language_probability, 4)},
        )
