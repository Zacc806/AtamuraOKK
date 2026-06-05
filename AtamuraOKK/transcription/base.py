"""Provider-agnostic transcription interface.

The pipeline depends only on :class:`Transcriber`, so the underlying engine
(self-hosted faster-whisper, a Kazakh-fine-tuned checkpoint, or a managed API)
can be swapped without touching ingestion or scoring.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class Segment:
    """One contiguous span of speech."""

    speaker: str  # "agent" | "customer" | "unknown"
    start: float  # seconds
    end: float  # seconds
    text: str


@dataclass(slots=True)
class TranscriptResult:
    """Full transcript of a single recording."""

    language: str
    full_text: str
    segments: list[Segment] = field(default_factory=list)
    model: str = ""
    # Engine-specific extras (e.g. detected-language probability).
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form (matches the ``transcripts`` table shape)."""
        return asdict(self)


@runtime_checkable
class Transcriber(Protocol):
    """Turns an audio file into a speaker-labeled, timestamped transcript."""

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        speaker: str = "unknown",
    ) -> TranscriptResult:
        """Transcribe one mono audio file/channel.

        :param audio_path: path to a single-channel audio file.
        :param language: ISO code to force, or ``None`` to auto-detect.
        :param speaker: label applied to every segment of this channel.
        """
        ...


@runtime_checkable
class AsyncTranscriber(Protocol):
    """A transcriber the worker can await, regardless of engine.

    Both providers satisfy this: the OpenAI provider is natively async; the
    faster-whisper provider wraps its CPU-bound decode in a thread.
    """

    async def transcribe_async(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        speaker: str = "unknown",
    ) -> TranscriptResult:
        """Transcribe one mono file/channel; see :meth:`Transcriber.transcribe`."""
        ...
