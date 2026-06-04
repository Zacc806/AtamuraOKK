"""Language-routed transcription: faster-whisper (ru) -> Yandex SpeechKit (kk/shala).

faster-whisper runs first (local, free) and detects the language. Russian stays;
Kazakh / "шала казахский" / low-confidence Russian is escalated to Yandex
SpeechKit (paid, better on Kazakh) — so the paid path only touches that subset.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from AtamuraOKK.scoring.language import has_kazakh_signal
from AtamuraOKK.transcription.base import Transcriber, TranscriptResult

_KAZAKH_LANG = "kk-KK"


class LanguageRoutedTranscriber:
    """Transcribe with faster-whisper, escalating Kazakh-ish audio to Yandex."""

    def __init__(
        self,
        *,
        primary: Transcriber,
        kazakh: Transcriber,
        confidence_threshold: float = 0.75,
    ) -> None:
        self._primary = primary
        self._kazakh = kazakh
        self._threshold = confidence_threshold

    def _should_escalate(self, result: TranscriptResult) -> bool:
        lang = (result.language or "").lower()
        prob = float(result.meta.get("language_probability") or 1.0)
        return (
            lang.startswith("kk")
            or has_kazakh_signal(result.full_text)
            or (lang.startswith("ru") and prob < self._threshold)
        )

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        speaker: str = "unknown",
    ) -> TranscriptResult:
        """Transcribe via faster-whisper; re-do Kazakh-ish audio on Yandex."""
        result = self._primary.transcribe(
            audio_path,
            language=language,
            speaker=speaker,
        )
        if self._should_escalate(result):
            logger.info(
                "transcription escalated to Yandex (lang={lang})",
                lang=result.language,
            )
            return self._kazakh.transcribe(
                audio_path,
                language=_KAZAKH_LANG,
                speaker=speaker,
            )
        return result


def build_transcriber() -> Transcriber:
    """Wire the production language-routed transcriber from settings.

    Russian -> OpenAI gpt-4o-transcribe (faster-whisper stays only for the
    offline WER spike). Kazakh / shala -> Yandex SpeechKit.
    """
    from AtamuraOKK.settings import settings  # noqa: PLC0415
    from AtamuraOKK.transcription.openai_transcribe import (  # noqa: PLC0415
        OpenAITranscriber,
    )
    from AtamuraOKK.transcription.yandex_speech import (  # noqa: PLC0415
        YandexSpeechKitTranscriber,
    )

    primary = OpenAITranscriber(
        api_key=settings.openai_api_key,
        model=settings.openai_transcribe_model,
    )
    kazakh = YandexSpeechKitTranscriber(
        api_key=settings.yandex_api_key,
        folder_id=settings.yandex_folder_id,
        model=settings.yandex_speechkit_model,
    )
    return LanguageRoutedTranscriber(
        primary=primary,
        kazakh=kazakh,
        confidence_threshold=settings.score_lang_confidence,
    )
