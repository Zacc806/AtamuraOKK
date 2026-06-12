"""Yandex SpeechKit v3 async transcription for meetings — self-contained.

The prepared mono OGG/Opus recording (see ``media.to_mono_opus``) goes inline
into one ``RecognizeFile`` request (≤ 60 MB — hours of Opus audio), we poll the
long-running operation, then assemble the streamed finals one per line — the
scorer's chunker splits on newlines.

Deliberately parallel to the call pipeline's ``transcription/yandex_*`` modules
rather than importing them: this automation stays off ``AtamuraOKK.settings``
(see ``config.py``), but reads the same ``ATAMURAOKK_YANDEX_*`` env vars and
service-account authorized key, so one Yandex setup serves both pipelines.
"""

from __future__ import annotations

# gRPC/yandexcloud/pyjwt deps live in the optional `yandex` group, so those
# imports are lazy and method-local — disable the top-level-import rule.
# ruff: noqa: PLC0415
import asyncio
import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from loguru import logger

from AtamuraOKK.scoring.meetings.config import REPO_ROOT, config

if TYPE_CHECKING:
    from collections.abc import Iterable

    from yandex.cloud.ai.stt.v3 import stt_pb2

    from AtamuraOKK.scoring.meetings.transcribe import TranscriptText

# Map a prepared-file suffix to the SpeechKit container type name.
_CONTAINER_BY_SUFFIX = {
    ".ogg": "OGG_OPUS",
    ".opus": "OGG_OPUS",
    ".wav": "WAV",
    ".mp3": "MP3",
}
# 60 MB inline limit for API v3 RecognizeFile (Yandex hard limit).
_MAX_INLINE_BYTES = 60 * 1024 * 1024
# Refresh the IAM token this many seconds before its stated expiry.
_REFRESH_MARGIN = 300
# JWT lifetime for the token exchange (max allowed is 1 h).
_JWT_TTL = 3600


class IamTokenProvider:
    """Caches an IAM token minted from the service-account authorized key."""

    def __init__(self) -> None:
        self._key_file = config.yandex_sa_key_file
        self._token = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def _signed_jwt(self) -> str:
        import jwt

        path = Path(self._key_file).expanduser()
        if not path.is_absolute():
            # Resolve against the repo root, not the CWD, so the worker finds
            # the key regardless of where it's launched.
            path = REPO_ROOT / path
        if not self._key_file or not path.is_file():
            raise RuntimeError(
                f"Yandex SA key file not found: {self._key_file!r} "
                "(ATAMURAOKK_YANDEX_SA_KEY_FILE).",
            )
        key = json.loads(path.read_text(encoding="utf-8"))
        now = int(time.time())
        payload = {
            "aud": config.yandex_iam_endpoint,
            "iss": key["service_account_id"],
            "iat": now,
            "exp": now + _JWT_TTL,
        }
        return jwt.encode(
            payload,
            key["private_key"],
            algorithm="PS256",
            headers={"kid": key["id"]},
        )

    def token(self) -> str:
        """Return a valid IAM token, refreshing if missing or near expiry."""
        with self._lock:
            if not self._token or time.time() >= self._expires_at - _REFRESH_MARGIN:
                resp = httpx.post(
                    config.yandex_iam_endpoint,
                    json={"jwt": self._signed_jwt()},
                    timeout=30.0,
                )
                resp.raise_for_status()
                self._token = str(resp.json()["iamToken"])
                # Tokens live ~12 h; refresh on our own clock.
                self._expires_at = time.time() + 11 * 3600
            return self._token


def _assemble_text(finals: Iterable[str]) -> str:
    """Join finals one per line (≈ utterance), dropping blanks.

    Chunking and the duplicate-line cleanup both split on newlines, so a
    space-joined transcript would be one giant "line" that can only be
    truncated, never chunked.
    """
    return "\n".join(t for t in (f.strip() for f in finals) if t)


class YandexTranscriber:
    """Transcribe one prepared mono recording via SpeechKit v3 async (ru + kk)."""

    # Tells `_prepare_audio` to downmix to OGG/Opus (fits the inline cap).
    audio_suffix = ".ogg"

    def __init__(self) -> None:
        self._iam = IamTokenProvider() if config.yandex_sa_key_file else None
        if self._iam is None and not config.yandex_secret_key:
            raise RuntimeError(
                "Yandex auth not configured: set ATAMURAOKK_YANDEX_SA_KEY_FILE "
                "(authorized key) or ATAMURAOKK_YANDEX_SECRET_KEY (API key).",
            )

    def _auth_metadata(self) -> tuple[tuple[str, str], ...]:
        """Auth header: Bearer IAM token if configured, else Api-Key."""
        if self._iam is not None:
            return (("authorization", f"Bearer {self._iam.token()}"),)
        return (("authorization", f"Api-Key {config.yandex_secret_key}"),)

    def _recognition_model(
        self, container_type: str
    ) -> stt_pb2.RecognitionModelOptions:
        from yandex.cloud.ai.stt.v3 import stt_pb2

        norm = (
            stt_pb2.TextNormalizationOptions.TEXT_NORMALIZATION_ENABLED
            if config.yandex_stt_normalize
            else stt_pb2.TextNormalizationOptions.TEXT_NORMALIZATION_DISABLED
        )
        ctype = getattr(stt_pb2.ContainerAudio, container_type)
        return stt_pb2.RecognitionModelOptions(
            model=config.yandex_stt_model,
            audio_format=stt_pb2.AudioFormatOptions(
                container_audio=stt_pb2.ContainerAudio(container_audio_type=ctype),
            ),
            text_normalization=stt_pb2.TextNormalizationOptions(
                text_normalization=norm,
                profanity_filter=False,
            ),
            language_restriction=stt_pb2.LanguageRestrictionOptions(
                restriction_type=stt_pb2.LanguageRestrictionOptions.WHITELIST,
                language_code=list(config.yandex_stt_languages),
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

        deadline = time.monotonic() + config.meetings_stt_timeout
        with grpc.secure_channel(
            config.yandex_operation_endpoint, grpc.ssl_channel_credentials()
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
                time.sleep(config.meetings_stt_poll_interval)

    def _collect_finals(self, operation_id: str, metadata: object) -> list[str]:
        """Stream GetRecognition finals in arrival order (mono: one channel)."""
        import grpc
        from yandex.cloud.ai.stt.v3 import (
            stt_service_pb2 as svc,
        )
        from yandex.cloud.ai.stt.v3 import (
            stt_service_pb2_grpc as svcg,
        )

        finals: list[str] = []
        with grpc.secure_channel(
            config.yandex_stt_endpoint, grpc.ssl_channel_credentials()
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
                elif event == "final" and not config.yandex_stt_normalize:
                    upd = resp.final
                else:
                    continue
                if upd.alternatives and upd.alternatives[0].text:
                    finals.append(upd.alternatives[0].text)
        return finals

    def _recognize_sync(self, audio_path: Path) -> TranscriptText:
        import grpc
        from yandex.cloud.ai.stt.v3 import (
            stt_pb2,
        )
        from yandex.cloud.ai.stt.v3 import (
            stt_service_pb2_grpc as svcg,
        )

        from AtamuraOKK.scoring.meetings.transcribe import TranscriptText

        content = audio_path.read_bytes()
        if len(content) > _MAX_INLINE_BYTES:
            raise RuntimeError(
                f"{audio_path.name} is {len(content)} bytes; exceeds the 60 MB "
                "inline async limit (would need bucket upload).",
            )
        container = _CONTAINER_BY_SUFFIX.get(audio_path.suffix.lower())
        if container is None:
            raise RuntimeError(
                f"unsupported container for Yandex STT: {audio_path.name} "
                "(ffmpeg downmix to OGG/Opus required)",
            )

        metadata = self._auth_metadata()
        with grpc.secure_channel(
            config.yandex_stt_endpoint, grpc.ssl_channel_credentials()
        ) as ch:
            stub = svcg.AsyncRecognizerStub(ch)
            operation = stub.RecognizeFile(
                stt_pb2.RecognizeFileRequest(
                    content=content,
                    recognition_model=self._recognition_model(container),
                ),
                metadata=metadata,
            )
        self._await_operation(operation.id, metadata)
        finals = self._collect_finals(operation.id, metadata)
        text = _assemble_text(finals)
        logger.debug(
            "  yandex recognized {n} finals, {c} chars", n=len(finals), c=len(text)
        )
        # Language is left "auto": the LLM scorer detects ru/kk from the text.
        return TranscriptText(text=text, language="auto")

    async def transcribe(self, audio_path: Path) -> TranscriptText:
        """Transcribe off the event loop (gRPC + polling are blocking)."""
        return await asyncio.to_thread(self._recognize_sync, audio_path)
