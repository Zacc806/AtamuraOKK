"""Yandex SpeechKit STT — Kazakh transcription path.

faster-whisper handles Russian for free; Kazakh / "шала казахский" is escalated
here (Yandex is better on Kazakh but paid, so we use it only on that subset).

Note: this uses the SpeechKit v1 sync recognize endpoint (short audio: <=30s,
<=1MB). Production long calls should move to the async ``recognizeFileAsync``
API via Object Storage — swap :meth:`_recognize` without touching the interface.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from AtamuraOKK.transcription.base import Segment, TranscriptResult

_RECOGNIZE_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"


class YandexSpeechKitTranscriber:
    """Transcribe Kazakh audio via Yandex SpeechKit (implements Transcriber)."""

    def __init__(
        self,
        *,
        api_key: str = "",
        folder_id: str = "",
        language: str = "kk-KK",
        model: str = "general",
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._folder_id = folder_id
        self._language = language
        self._model = model
        self._client = client

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=120.0)
        return self._client

    def _recognize(self, audio: bytes, language: str) -> str:
        client = self._ensure_client()
        params = {"folderId": self._folder_id, "lang": language, "topic": self._model}
        headers = {"Authorization": f"Api-Key {self._api_key}"}
        resp = client.post(
            _RECOGNIZE_URL,
            params=params,
            headers=headers,
            content=audio,
        )
        resp.raise_for_status()
        return str(resp.json().get("result", ""))

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        speaker: str = "unknown",
    ) -> TranscriptResult:
        """Transcribe one mono audio file via SpeechKit (see :class:`Transcriber`)."""
        lang = language or self._language
        text = self._recognize(audio_path.read_bytes(), lang).strip()
        segments = (
            [Segment(speaker=speaker, start=0.0, end=0.0, text=text)] if text else []
        )
        return TranscriptResult(
            language="kk",
            full_text=text,
            segments=segments,
            model="yandex/speechkit",
            meta={"channels": 1, "provider": "yandex_speechkit"},
        )
