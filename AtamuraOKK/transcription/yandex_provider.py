"""Yandex SpeechKit v3 implementation of :class:`Transcriber`.

Transcribes one mono channel per call via the v3 *streaming* gRPC API
(``stt.api.cloud.yandex.net``), authenticated with a service-account API key.
Streaming is used (not the async/long-running API) because it accepts raw audio
inline — no Yandex Object Storage bucket is required, so a local 16 kHz mono WAV
channel can be sent straight from disk.

``audio_processing_type=FULL_DATA`` makes the service recognize the whole stream
and return final results once, rather than the real-time partials. Speaker comes
from the channel, not diarization; language is detected downstream from the text.

Optional dependency (``uv sync --group yandex``): ``grpcio`` + ``yandexcloud``
(the latter ships the generated v3 stubs), imported lazily so the rest of the
pipeline never pulls in gRPC.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from AtamuraOKK.settings import settings
from AtamuraOKK.transcription.base import Segment, TranscriptResult

if TYPE_CHECKING:
    from collections.abc import Iterator

    from yandex.cloud.ai.stt.v3 import stt_pb2

# ~0.5 s of 16 kHz / 16-bit mono audio per chunk; well under SpeechKit limits.
_CHUNK_BYTES = 16_000


class YandexSpeechKitTranscriber:
    """Transcribe a single mono WAV channel via SpeechKit v3 streaming."""

    # SpeechKit recognizes Kazakh (kk-KZ), so the worker lets kk calls advance
    # to TRANSCRIBED instead of parking them at PENDING_KK.
    handles_kazakh = True

    def __init__(
        self,
        model: str | None = None,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        self.model = model or settings.yandex_stt_model
        self.endpoint = endpoint or settings.yandex_stt_endpoint
        self._api_key = api_key or settings.yandex_secret_key
        # Prefer IAM-token (Bearer) auth from a SA authorized key when present;
        # it has no API-key scope restriction.
        self._iam = None
        if settings.yandex_sa_key_file:
            from AtamuraOKK.transcription.yandex_iam import (  # noqa: PLC0415
                IamTokenProvider,
            )

            self._iam = IamTokenProvider()

    def _auth_metadata(self) -> tuple[tuple[str, str], ...]:
        """Auth header: Bearer IAM token if configured, else Api-Key."""
        if self._iam is not None:
            return (("authorization", f"Bearer {self._iam.token()}"),)
        if not self._api_key:
            raise RuntimeError(
                "Yandex auth not configured: set ATAMURAOKK_YANDEX_SA_KEY_FILE "
                "(authorized key) or ATAMURAOKK_YANDEX_SECRET_KEY (API key).",
            )
        return (("authorization", f"Api-Key {self._api_key}"),)

    def _streaming_options(
        self, *, sample_rate: int, channels: int
    ) -> stt_pb2.StreamingOptions:
        from yandex.cloud.ai.stt.v3 import stt_pb2  # noqa: PLC0415

        norm = (
            stt_pb2.TextNormalizationOptions.TEXT_NORMALIZATION_ENABLED
            if settings.yandex_stt_normalize
            else stt_pb2.TextNormalizationOptions.TEXT_NORMALIZATION_DISABLED
        )
        lang = stt_pb2.LanguageRestrictionOptions(
            restriction_type=stt_pb2.LanguageRestrictionOptions.WHITELIST,
            language_code=list(settings.yandex_stt_languages),
        )
        return stt_pb2.StreamingOptions(
            recognition_model=stt_pb2.RecognitionModelOptions(
                model=self.model,
                audio_format=stt_pb2.AudioFormatOptions(
                    raw_audio=stt_pb2.RawAudio(
                        audio_encoding=stt_pb2.RawAudio.LINEAR16_PCM,
                        sample_rate_hertz=sample_rate,
                        audio_channel_count=channels,
                    ),
                ),
                text_normalization=stt_pb2.TextNormalizationOptions(
                    text_normalization=norm,
                    profanity_filter=False,
                ),
                language_restriction=lang,
                # Recognize the whole file at once (vs. real-time partials).
                audio_processing_type=stt_pb2.RecognitionModelOptions.FULL_DATA,
            ),
        )

    def _requests(
        self,
        pcm: bytes,
        *,
        sample_rate: int,
        channels: int,
    ) -> Iterator[stt_pb2.StreamingRequest]:
        """Yield the session options, then the raw-PCM audio chunks."""
        from yandex.cloud.ai.stt.v3 import stt_pb2  # noqa: PLC0415

        yield stt_pb2.StreamingRequest(
            session_options=self._streaming_options(
                sample_rate=sample_rate,
                channels=channels,
            ),
        )
        for off in range(0, len(pcm), _CHUNK_BYTES):
            yield stt_pb2.StreamingRequest(
                chunk=stt_pb2.AudioChunk(data=pcm[off : off + _CHUNK_BYTES]),
            )

    def _recognize(
        self,
        pcm: bytes,
        *,
        sample_rate: int,
        channels: int,
        speaker: str,
    ) -> list[str]:
        """Run the blocking gRPC stream and collect final-text fragments."""
        import grpc  # noqa: PLC0415
        from yandex.cloud.ai.stt.v3 import stt_service_pb2_grpc  # noqa: PLC0415

        metadata = self._auth_metadata()
        cred = grpc.ssl_channel_credentials()
        with grpc.secure_channel(self.endpoint, cred) as channel:
            stub = stt_service_pb2_grpc.RecognizerStub(channel)
            responses = stub.RecognizeStreaming(
                self._requests(pcm, sample_rate=sample_rate, channels=channels),
                metadata=metadata,
            )
            texts: list[str] = []
            for resp in responses:
                event = resp.WhichOneof("Event")
                # With normalization on, the normalized text arrives as a
                # final_refinement; fall back to the plain `final` otherwise.
                if event == "final_refinement":
                    alts = resp.final_refinement.normalized_text.alternatives
                    if alts and alts[0].text:
                        texts.append(alts[0].text)
                elif event == "final" and not settings.yandex_stt_normalize:
                    alts = resp.final.alternatives
                    if alts and alts[0].text:
                        texts.append(alts[0].text)
            logger.debug(
                "  [{speaker}] SpeechKit returned {n} final fragment(s)",
                speaker=speaker,
                n=len(texts),
            )
            return texts

    async def transcribe_async(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        speaker: str = "unknown",
    ) -> TranscriptResult:
        """Transcribe one mono WAV channel; see :class:`Transcriber`."""
        return await asyncio.to_thread(
            self.transcribe,
            audio_path,
            language=language,
            speaker=speaker,
        )

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        speaker: str = "unknown",
    ) -> TranscriptResult:
        """Transcribe one mono WAV file/channel (see :class:`Transcriber`)."""
        with wave.open(str(audio_path), "rb") as wav:
            sample_rate = wav.getframerate()
            channels = wav.getnchannels()
            pcm = wav.readframes(wav.getnframes())

        texts = self._recognize(
            pcm,
            sample_rate=sample_rate,
            channels=channels,
            speaker=speaker,
        )
        text = " ".join(t.strip() for t in texts if t.strip()).strip()
        return TranscriptResult(
            language="",  # filled in after RU/KK detection on the combined text
            full_text=text,
            segments=[Segment(speaker=speaker, start=0.0, end=0.0, text=text)]
            if text
            else [],
            model=f"yandex/{self.model}",
        )
