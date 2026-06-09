"""Select the transcription engine from settings.

Keeps the worker provider-agnostic: it asks for an :class:`AsyncTranscriber`
and never names a concrete engine.
"""

from __future__ import annotations

from loguru import logger

from AtamuraOKK.settings import settings
from AtamuraOKK.transcription.base import AsyncTranscriber


def get_transcriber() -> AsyncTranscriber:
    """Return the configured transcriber ("whisper" local, or "openai")."""
    provider = settings.transcribe_provider.lower()
    if provider == "openai":
        from AtamuraOKK.transcription.openai_provider import (  # noqa: PLC0415
            OpenAITranscriber,
        )

        logger.info("Transcriber: OpenAI {m}", m=settings.openai_transcribe_model)
        return OpenAITranscriber()
    if provider == "whisper":
        from AtamuraOKK.transcription.whisper import (  # noqa: PLC0415
            FasterWhisperTranscriber,
        )

        logger.info(
            "Transcriber: faster-whisper {m} (device={d}, compute={c})",
            m=settings.whisper_model,
            d=settings.whisper_device,
            c=settings.whisper_compute_type,
        )
        return FasterWhisperTranscriber()
    if provider in ("yandex", "speechkit"):
        if settings.yandex_stt_mode.lower() == "async":
            from AtamuraOKK.transcription.yandex_async_provider import (  # noqa: PLC0415
                YandexAsyncTranscriber,
            )

            logger.info(
                "Transcriber: Yandex SpeechKit v3 async ({m})",
                m=settings.yandex_stt_model,
            )
            return YandexAsyncTranscriber()
        from AtamuraOKK.transcription.yandex_provider import (  # noqa: PLC0415
            YandexSpeechKitTranscriber,
        )

        logger.info(
            "Transcriber: Yandex SpeechKit v3 streaming ({m})",
            m=settings.yandex_stt_model,
        )
        return YandexSpeechKitTranscriber()
    msg = (
        f"Unknown transcribe_provider {provider!r} "
        "(use 'whisper', 'openai', or 'yandex')."
    )
    raise ValueError(msg)
