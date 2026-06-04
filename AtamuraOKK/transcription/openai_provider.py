"""OpenAI gpt-4o-transcribe implementation of :class:`Transcriber`.

Transcribes one mono channel per call (best-accuracy text; no timestamps, per
the model's API). Speaker comes from the channel, not diarization. Language is
detected downstream from the text (the model returns no language field).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from AtamuraOKK.settings import settings
from AtamuraOKK.transcription.base import Segment, TranscriptResult

if TYPE_CHECKING:
    from openai import AsyncOpenAI


class OpenAITranscriber:
    """Transcribe a single mono audio file via the OpenAI audio API."""

    def __init__(
        self,
        model: str | None = None,
        *,
        api_key: str | None = None,
    ) -> None:
        self.model = model or settings.openai_transcribe_model
        self._api_key = api_key or settings.openai_api_key
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            from openai import AsyncOpenAI  # noqa: PLC0415

            if not self._api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY is not set (ATAMURAOKK_OPENAI_API_KEY).",
                )
            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def transcribe_async(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        speaker: str = "unknown",
    ) -> TranscriptResult:
        """Transcribe one mono file; language detection happens downstream."""
        client = self._get_client()
        with audio_path.open("rb") as fh:
            resp = await client.audio.transcriptions.create(
                model=self.model,
                file=fh,
                response_format="json",
            )
        text = (resp.text or "").strip()
        logger.debug("Transcribed {p} ({n} chars)", p=audio_path.name, n=len(text))
        return TranscriptResult(
            language="",  # filled in after RU/KK detection on the combined text
            full_text=text,
            segments=[Segment(speaker=speaker, start=0.0, end=0.0, text=text)]
            if text
            else [],
            model=f"openai/{self.model}",
        )
