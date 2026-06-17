"""Yandex SpeechKit v3 **async** recognition implementation.

Unlike the streaming provider (`yandex_provider.py`), async recognition has no
5-minute-per-session cap (handles audio up to 4 h) and accepts a **multi-channel
container inline** — so the whole stereo recording goes in one request and comes
back with per-channel results (`channel_tag`). That removes the ffmpeg channel
split and avoids the streaming truncation on long calls.

Each ``final``/``final_refinement`` is one recognized utterance carrying a time
span; we keep them **as separate, timestamped segments** and interleave both
channels by start time, so the stored transcript is a real manager↔client
dialogue rather than two glued per-channel blobs.

For **mono** recordings (one channel, no acoustic separation) we ask SpeechKit
to label speakers (`speaker_labeling`); the per-speaker turn boundaries arrive as
``SpeakerAnalysis`` ``LAST_UTTERANCE`` windows, which we use to attribute each
utterance to a speaker. If labeling yields nothing usable we fall back to one
undifferentiated segment, so a mono call never regresses below today's behaviour.

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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from AtamuraOKK.audio import probe_channels
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
# Neutral side labels for the mono speaker-labeling path (role is decided later,
# by content, in scoring). Stereo uses the fixed channel convention in
# `settings.stereo_agent_channel` instead — Voximplant's two channels carry the
# same role every call, so the side is read from the channel, not guessed.
_SIDE_SPEAKERS = ("agent", "customer")


@dataclass(slots=True, frozen=True)
class _Utterance:
    """One recognized final: which channel said it, its time span, and text."""

    channel_tag: str
    start_ms: int
    end_ms: int
    text: str


@dataclass(slots=True, frozen=True)
class _SpeakerWindow:
    """A ``SpeakerAnalysis`` LAST_UTTERANCE span attributed to one speaker."""

    speaker_tag: str
    start_ms: int
    end_ms: int


def _utterance_span(alt: Any) -> tuple[int, int]:
    """Best (start, end) in ms for an alternative, falling back to its words."""
    start = alt.start_time_ms
    end = alt.end_time_ms
    if not start and alt.words:
        start = alt.words[0].start_time_ms
    if not end and alt.words:
        end = alt.words[-1].end_time_ms
    return start, end


def _ordered_dialogue(segments: list[Segment]) -> list[Segment]:
    """Sort segments by start time and merge consecutive same-speaker turns."""
    kept = sorted(
        (s for s in segments if s.text.strip()),
        key=lambda s: (s.start, s.end),
    )
    merged: list[Segment] = []
    for s in kept:
        if merged and merged[-1].speaker == s.speaker:
            prev = merged[-1]
            prev.text = f"{prev.text} {s.text}".strip()
            prev.end = max(prev.end, s.end)
        else:
            merged.append(Segment(s.speaker, s.start, s.end, s.text))
    return merged


def _channel_side(position: int, total: int, tag: str) -> str:
    """Side label for the channel at sorted ``position`` of ``total`` channels.

    The common (stereo) case maps the configured agent channel to "agent" and the
    other to "customer"; degenerate channel counts fall back to neutral labels.
    """
    if total == 2:
        return "agent" if position == settings.stereo_agent_channel else "customer"
    if position < len(_SIDE_SPEAKERS):
        return _SIDE_SPEAKERS[position]
    return f"channel{tag}"


def _segments_by_channel(utterances: list[_Utterance]) -> list[Segment]:
    """Stereo: map each channel to a side and interleave utterances by time."""
    tags = sorted({u.channel_tag for u in utterances})
    mapping = {tag: _channel_side(i, len(tags), tag) for i, tag in enumerate(tags)}
    segs = [
        Segment(mapping[u.channel_tag], u.start_ms / 1000, u.end_ms / 1000, u.text)
        for u in utterances
    ]
    return _ordered_dialogue(segs)


def _best_speaker(utt: _Utterance, windows: list[_SpeakerWindow]) -> str | None:
    """Speaker whose LAST_UTTERANCE window overlaps this utterance the most."""
    best: str | None = None
    best_overlap = 0
    for w in windows:
        overlap = min(utt.end_ms, w.end_ms) - max(utt.start_ms, w.start_ms)
        if overlap > best_overlap:
            best_overlap = overlap
            best = w.speaker_tag
    return best


def _single_blob(utterances: list[_Utterance]) -> list[Segment]:
    """Mono fallback (no speaker windows): one undifferentiated segment."""
    text = " ".join(u.text for u in utterances if u.text).strip()
    if not text:
        return []
    end = max((u.end_ms for u in utterances), default=0) / 1000
    return [Segment("unknown", 0.0, end, text)]


def _segments_by_speaker(
    utterances: list[_Utterance],
    windows: list[_SpeakerWindow],
) -> list[Segment]:
    """Mono: attribute each utterance to a speaker via overlap with its window."""
    if not windows:
        return _single_blob(utterances)
    tags = sorted({w.speaker_tag for w in windows})
    mapping = {
        tag: _SIDE_SPEAKERS[i] if i < len(_SIDE_SPEAKERS) else f"speaker{tag}"
        for i, tag in enumerate(tags)
    }
    segs = [
        Segment(
            mapping.get(_best_speaker(u, windows) or "", "unknown"),
            u.start_ms / 1000,
            u.end_ms / 1000,
            u.text,
        )
        for u in utterances
    ]
    return _ordered_dialogue(segs)


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

    def _speaker_labeling(self, channels: int) -> stt_pb2.SpeakerLabelingOptions:
        """Enable speaker labeling only for mono (stereo separates by channel)."""
        from yandex.cloud.ai.stt.v3 import stt_pb2

        enabled = channels < 2 and settings.yandex_speaker_labeling
        value = (
            stt_pb2.SpeakerLabelingOptions.SPEAKER_LABELING_ENABLED
            if enabled
            else stt_pb2.SpeakerLabelingOptions.SPEAKER_LABELING_DISABLED
        )
        return stt_pb2.SpeakerLabelingOptions(speaker_labeling=value)

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
    ) -> tuple[list[_Utterance], list[_SpeakerWindow]]:
        """Stream GetRecognition finals as per-utterance spans + speaker windows."""
        import grpc
        from yandex.cloud.ai.stt.v3 import (
            stt_pb2,
        )
        from yandex.cloud.ai.stt.v3 import (
            stt_service_pb2 as svc,
        )
        from yandex.cloud.ai.stt.v3 import (
            stt_service_pb2_grpc as svcg,
        )

        last_utterance = stt_pb2.SpeakerAnalysis.LAST_UTTERANCE
        utterances: list[_Utterance] = []
        windows: list[_SpeakerWindow] = []
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
                if event == "speaker_analysis":
                    sa = resp.speaker_analysis
                    if sa.window_type == last_utterance:
                        b = sa.speech_boundaries
                        windows.append(
                            _SpeakerWindow(
                                str(sa.speaker_tag), b.start_time_ms, b.end_time_ms
                            ),
                        )
                    continue
                if event == "final_refinement":
                    upd = resp.final_refinement.normalized_text
                elif event == "final" and not settings.yandex_stt_normalize:
                    upd = resp.final
                else:
                    continue
                if not upd.alternatives:
                    continue
                alt = upd.alternatives[0]
                text = alt.text.strip()
                if not text:
                    continue
                start_ms, end_ms = _utterance_span(alt)
                utterances.append(
                    _Utterance(str(upd.channel_tag), start_ms, end_ms, text),
                )
        return utterances, windows

    def _recognize_file(
        self, content: bytes, container_type: str, channels: int
    ) -> TranscriptResult:
        import grpc
        from yandex.cloud.ai.stt.v3 import (
            stt_pb2,
        )
        from yandex.cloud.ai.stt.v3 import (
            stt_service_pb2_grpc as svcg,
        )

        metadata = self._auth_metadata()
        request = stt_pb2.RecognizeFileRequest(
            content=content,
            recognition_model=self._recognition_model(container_type),
        )
        request.speaker_labeling.CopyFrom(self._speaker_labeling(channels))
        with grpc.secure_channel(
            self.stt_endpoint, grpc.ssl_channel_credentials()
        ) as ch:
            stub = svcg.AsyncRecognizerStub(ch)
            operation = stub.RecognizeFile(request, metadata=metadata)
        self._await_operation(operation.id, metadata)
        utterances, windows = self._collect_results(operation.id, metadata)

        if channels >= 2:
            segments = _segments_by_channel(utterances)
        else:
            segments = _segments_by_speaker(utterances, windows)
        full_text = "\n\n".join(
            f"[{s.speaker.upper()}]\n{s.text}" for s in segments
        ).strip()
        logger.debug(
            "  async recognized {n} segment(s) over {ch} channel(s), {c} chars",
            n=len(segments),
            ch=channels,
            c=len(full_text),
        )
        return TranscriptResult(
            language=detect_language(full_text),
            full_text=full_text,
            segments=segments,
            model=f"yandex-async/{self.model}",
            meta={"channels": channels, "stereo": channels >= 2, "mode": "async"},
        )

    async def transcribe_file(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
    ) -> TranscriptResult:
        """Transcribe a whole recording; per-channel (stereo) or labeled (mono)."""
        content = audio_path.read_bytes()
        if len(content) > _MAX_INLINE_BYTES:
            raise RuntimeError(
                f"{audio_path.name} is {len(content)} bytes; exceeds the 60 MB "
                "inline async limit (would need bucket upload).",
            )
        container = _CONTAINER_BY_SUFFIX.get(audio_path.suffix.lower(), "MP3")
        channels = probe_channels(audio_path)
        return await asyncio.to_thread(
            self._recognize_file, content, container, channels
        )

    async def transcribe_async(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        speaker: str = "unknown",
    ) -> TranscriptResult:
        """Single-channel entry point (kept for interface compatibility).

        The worker uses :meth:`transcribe_file` for this provider; this fallback
        transcribes one file and labels every segment with ``speaker``.
        """
        res = await self.transcribe_file(audio_path, language=language)
        segs = [Segment(speaker=speaker, start=0.0, end=0.0, text=res.full_text)]
        return TranscriptResult(
            language=res.language,
            full_text=res.full_text,
            segments=segs if res.full_text else [],
            model=res.model,
        )
