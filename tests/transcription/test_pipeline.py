"""Tests for the mono transcription path (stereo needs ffmpeg, not unit-tested)."""

from __future__ import annotations

from pathlib import Path

from AtamuraOKK.transcription.base import Segment, TranscriptResult
from AtamuraOKK.transcription.pipeline import transcribe_file


class _FakeTranscriber:
    """A transcriber that returns a fixed result and records the speaker label."""

    def __init__(self) -> None:
        self.last_speaker = ""

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        speaker: str = "unknown",
    ) -> TranscriptResult:
        """Return a canned one-segment transcript."""
        self.last_speaker = speaker
        return TranscriptResult(
            language="ru",
            full_text="привет",
            segments=[Segment(speaker=speaker, start=0.0, end=1.0, text="привет")],
            model="fake",
            meta={},
        )


def test_mono_transcription_passthrough() -> None:
    """The mono path transcribes the whole file as a single 'unknown' speaker."""
    transcriber = _FakeTranscriber()
    result = transcribe_file(transcriber, Path("call.mp3"), is_stereo=False)
    assert result.full_text == "привет"
    assert result.meta["stereo"] is False
    assert transcriber.last_speaker == "unknown"
