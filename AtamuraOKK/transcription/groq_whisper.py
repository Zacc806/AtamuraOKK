"""Groq Whisper implementation of :class:`Transcriber` (production transcription).

Groq runs ``whisper-large-v3`` as an HTTP API, so this class loads no local
model. Mirrors the shape of :class:`FasterWhisperTranscriber`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from AtamuraOKK.transcription.base import Segment, TranscriptResult

if TYPE_CHECKING:
    from groq import Groq


def _field(segment: object, name: str) -> Any:
    """Read a field from a Groq segment, whether it is a dict or an object."""
    if isinstance(segment, dict):
        return segment.get(name)
    return getattr(segment, name, None)


class GroqWhisperTranscriber:
    """Transcribe one mono audio file/channel via Groq Whisper."""

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = "whisper-large-v3",
        client: Groq | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self._client = client

    def _ensure_client(self) -> Groq:
        if self._client is None:
            from groq import Groq  # noqa: PLC0415

            self._client = Groq(api_key=self.api_key)
        return self._client

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        speaker: str = "unknown",
    ) -> TranscriptResult:
        """Transcribe one mono audio file/channel (see :class:`Transcriber`)."""
        client = self._ensure_client()
        audio_bytes = audio_path.read_bytes()
        kwargs: dict[str, Any] = {
            "file": (audio_path.name, audio_bytes),
            "model": self.model,
            "response_format": "verbose_json",
        }
        if language is not None:
            kwargs["language"] = language
        resp = client.audio.transcriptions.create(**kwargs)

        segments: list[Segment] = []
        texts: list[str] = []
        for seg in getattr(resp, "segments", None) or []:
            text = str(_field(seg, "text") or "").strip()
            segments.append(
                Segment(
                    speaker=speaker,
                    start=round(float(_field(seg, "start") or 0.0), 2),
                    end=round(float(_field(seg, "end") or 0.0), 2),
                    text=text,
                ),
            )
            texts.append(text)

        full_text = " ".join(texts).strip() or str(getattr(resp, "text", "") or "")
        return TranscriptResult(
            language=str(getattr(resp, "language", "") or "auto"),
            full_text=full_text,
            segments=segments,
            model=f"groq/{self.model}",
            meta={"channels": 1},
        )
