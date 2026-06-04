"""OpenAI implementation of :class:`Transcriber` (Russian production transcription).

Uses ``gpt-4o-transcribe`` via the OpenAI audio API. The model does not return a
language label, so we infer ru/kk from the text (Kazakh letters/words) — enough
for the language router to escalate Kazakh-ish audio to Yandex SpeechKit.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from AtamuraOKK.scoring.language import has_kazakh_signal
from AtamuraOKK.transcription.base import TranscriptResult

if TYPE_CHECKING:
    from openai import OpenAI


class OpenAITranscriber:
    """Transcribe one mono audio file/channel via OpenAI (implements Transcriber)."""

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = "gpt-4o-transcribe",
        client: OpenAI | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self._client = client

    def _ensure_client(self) -> OpenAI:
        if self._client is None:
            from openai import OpenAI  # noqa: PLC0415

            self._client = OpenAI(api_key=self.api_key)
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
        kwargs: dict[str, Any] = {
            "model": self.model,
            "file": (audio_path.name, audio_path.read_bytes()),
        }
        if language is not None:
            kwargs["language"] = language
        resp = client.audio.transcriptions.create(**kwargs)

        full_text = str(getattr(resp, "text", "") or "").strip()
        detected = language or ("kk" if has_kazakh_signal(full_text) else "ru")
        return TranscriptResult(
            language=detected,
            full_text=full_text,
            segments=[],
            model=f"openai/{self.model}",
            meta={"channels": 1},
        )
