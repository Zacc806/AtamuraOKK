"""Yandex SpeechKit v3 **async** recognition implementation.

Unlike the streaming provider (`yandex_provider.py`), async recognition has no
5-minute-per-session cap (handles audio up to 4 h) and accepts a **multi-channel
container inline** — so the whole stereo recording goes in one request and comes
back with per-channel results (`channel_tag`). That removes the ffmpeg channel
split and avoids the streaming truncation on long calls.

Flow (`AsyncRecognizer`): ``RecognizeFile`` (inline ``content`` ≤ 60 MB) returns
a long-running `Operation`; we poll the Operations API until it's done, then
``GetRecognition`` server-streams the finals. Auth is the same Bearer IAM token.

Exposes ``transcribe_file`` (whole recording) and sets ``wants_full_file`` so the
worker hands it the original audio instead of pre-split mono channels.
"""

from __future__ import annotations

# gRPC/yandexcloud deps are optional (the `yandex` group), so every Yandex import
# is lazy and method-local — disable the "import at top level" rule file-wide.
# ruff: noqa: PLC0415
import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from AtamuraOKK.settings import settings
from AtamuraOKK.transcription.base import Segment, TranscriptResult
from AtamuraOKK.transcription.language import detect_language

if TYPE_CHECKING:
    from yandex.cloud.ai.stt.v3 import stt_pb2

    from AtamuraOKK.transcription.yandex_iam import IamTokenProvider

# Map a file suffix to the SpeechKit container type name.
_CONTAINER_BY_SUFFIX = {
    ".mp3": "MP3",
    ".wav": "WAV",
    ".ogg": "OGG_OPUS",
    ".oga": "OGG_OPUS",
    ".opus": "OGG_OPUS",
}
# 60 MB inline limit for API v3 RecognizeFile (Yandex hard limit).
_MAX_INLINE_BYTES = 60 * 1024 * 1024


class YandexAsyncTranscriber:
    """Transcribe a whole recording via SpeechKit v3 async recognition."""

    handles_kazakh = True
    # Tell the worker to pass the original (stereo) file, not split channels.
    wants_full_file = True

    def __init__(
        self,
        model: str | None = None,
        *,
        stt_endpoint: str | None = None,
        operation_endpoint: str | None = None,
    ) -> None:
        self.model = model or settings.yandex_stt_model
        self.stt_endpoint = stt_endpoint or settings.yandex_stt_endpoint
        self.operation_endpoint = (
            operation_endpoint or settings.yandex_operation_endpoint
        )
        self._iam: IamTokenProvider | None = None
        if settings.yandex_sa_key_file:
            from AtamuraOKK.transcription.yandex_iam import (
                IamTokenProvider,
            )

            self._iam = IamTokenProvider()
        self._api_key = settings.yandex_secret_key

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

    def _recognition_model(
        self, container_type: str
    ) -> stt_pb2.RecognitionModelOptions:
        from yandex.cloud.ai.stt.v3 import stt_pb2

        norm = (
            stt_pb2.TextNormalizationOptions.TEXT_NORMALIZATION_ENABLED
            if settings.yandex_stt_normalize
            else stt_pb2.TextNormalizationOptions.TEXT_NORMALIZATION_DISABLED
        )
        ctype = getattr(stt_pb2.ContainerAudio, container_type)
        return stt_pb2.RecognitionModelOptions(
            model=self.model,
            audio_format=stt_pb2.AudioFormatOptions(
                container_audio=stt_pb2.ContainerAudio(container_audio_type=ctype),
            ),
            text_normalization=stt_pb2.TextNormalizationOptions(
                text_normalization=norm,
                profanity_filter=False,
            ),
            language_restriction=stt_pb2.LanguageRestrictionOptions(
                restriction_type=stt_pb2.LanguageRestrictionOptions.WHITELIST,
                language_code=list(settings.yandex_stt_languages),
            ),
            audio_processing_type=stt_pb2.RecognitionModelOptions.FULL_DATA,
        )

    def _await_operation(self, operation_id: str, metadata: object) -> None:
        """Poll the Operations API until the recognition operation is done."""
        import grpc
        from yandex.cloud.operation import (
            operation_service_pb2 as op,
        )
        from yandex.cloud.operation import (
            operation_service_pb2_grpc as opg,
        )

        deadline = time.monotonic() + settings.yandex_async_timeout
        with grpc.secure_channel(
            self.operation_endpoint, grpc.ssl_channel_credentials()
        ) as ch:
            stub = opg.OperationServiceStub(ch)
            while True:
                operation = stub.Get(
                    op.GetOperationRequest(operation_id=operation_id),
                    metadata=metadata,
                )
                if operation.done:
                    if operation.HasField("error"):
                        msg = f"operation error: {operation.error.message}"
                        raise RuntimeError(msg)
                    return
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"recognition operation {operation_id} timed out",
                    )
                time.sleep(settings.yandex_async_poll_interval)

    def _collect_results(
        self, operation_id: str, metadata: object
    ) -> dict[str, list[str]]:
        """Stream GetRecognition finals, grouped by channel_tag in arrival order."""
        import grpc
        from yandex.cloud.ai.stt.v3 import (
            stt_service_pb2 as svc,
        )
        from yandex.cloud.ai.stt.v3 import (
            stt_service_pb2_grpc as svcg,
        )

        by_channel: dict[str, list[str]] = {}
        with grpc.secure_channel(
            self.stt_endpoint, grpc.ssl_channel_credentials()
        ) as ch:
            stub = svcg.AsyncRecognizerStub(ch)
            responses = stub.GetRecognition(
                svc.GetRecognitionRequest(operation_id=operation_id),
                metadata=metadata,
            )
            for resp in responses:
                event = resp.WhichOneof("Event")
                if event == "final_refinement":
                    upd = resp.final_refinement.normalized_text
                elif event == "final" and not settings.yandex_stt_normalize:
                    upd = resp.final
                else:
                    continue
                if upd.alternatives and upd.alternatives[0].text:
                    by_channel.setdefault(upd.channel_tag, []).append(
                        upd.alternatives[0].text,
                    )
        return by_channel

    def _recognize_file(self, content: bytes, container_type: str) -> TranscriptResult:
        import grpc
        from yandex.cloud.ai.stt.v3 import (
            stt_pb2,
        )
        from yandex.cloud.ai.stt.v3 import (
            stt_service_pb2_grpc as svcg,
        )

        metadata = self._auth_metadata()
        with grpc.secure_channel(
            self.stt_endpoint, grpc.ssl_channel_credentials()
        ) as ch:
            stub = svcg.AsyncRecognizerStub(ch)
            operation = stub.RecognizeFile(
                stt_pb2.RecognizeFileRequest(
                    content=content,
                    recognition_model=self._recognition_model(container_type),
                ),
                metadata=metadata,
            )
        self._await_operation(operation.id, metadata)
        by_channel = self._collect_results(operation.id, metadata)

        # Map channel tags to speakers: lowest tag = agent (ch0), next = customer.
        speakers = ("agent", "customer")
        segments: list[Segment] = []
        for idx, tag in enumerate(sorted(by_channel)):
            speaker = speakers[idx] if idx < len(speakers) else f"channel{tag}"
            text = " ".join(t.strip() for t in by_channel[tag] if t.strip()).strip()
            if text:
                segments.append(
                    Segment(speaker=speaker, start=0.0, end=0.0, text=text),
                )
        full_text = "\n\n".join(
            f"[{s.speaker.upper()}]\n{s.text}" for s in segments
        ).strip()
        logger.debug(
            "  async recognized {n} channel(s), {c} chars",
            n=len(segments),
            c=len(full_text),
        )
        return TranscriptResult(
            language=detect_language(full_text),
            full_text=full_text,
            segments=segments,
            model=f"yandex-async/{self.model}",
            meta={"channels": len(segments), "mode": "async"},
        )

    async def transcribe_file(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
    ) -> TranscriptResult:
        """Transcribe a whole (stereo) recording; per-channel speaker labels."""
        content = audio_path.read_bytes()
        if len(content) > _MAX_INLINE_BYTES:
            raise RuntimeError(
                f"{audio_path.name} is {len(content)} bytes; exceeds the 60 MB "
                "inline async limit (would need bucket upload).",
            )
        container = _CONTAINER_BY_SUFFIX.get(audio_path.suffix.lower(), "MP3")
        return await asyncio.to_thread(self._recognize_file, content, container)

    async def transcribe_async(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        speaker: str = "unknown",
    ) -> TranscriptResult:
        """Single-channel entry point (kept for interface compatibility).

        The worker uses :meth:`transcribe_file` for this provider; this fallback
        transcribes one channel and labels every segment with ``speaker``.
        """
        res = await self.transcribe_file(audio_path, language=language)
        segs = [Segment(speaker=speaker, start=0.0, end=0.0, text=res.full_text)]
        return TranscriptResult(
            language=res.language,
            full_text=res.full_text,
            segments=segs if res.full_text else [],
            model=res.model,
        )
