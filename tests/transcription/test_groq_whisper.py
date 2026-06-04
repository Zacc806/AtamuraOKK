"""Tests for the Groq Whisper transcriber (mocked client)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from AtamuraOKK.transcription.groq_whisper import GroqWhisperTranscriber


def _fake_client(segments: list[dict[str, object]]) -> SimpleNamespace:
    """A stand-in Groq client returning a fixed verbose_json transcription."""

    def _create(**_: object) -> SimpleNamespace:
        return SimpleNamespace(text="привет как дела", language="ru", segments=segments)

    transcriptions = SimpleNamespace(create=_create)
    return SimpleNamespace(audio=SimpleNamespace(transcriptions=transcriptions))


def test_maps_segments_and_language(tmp_path: Path) -> None:
    """Groq segments map to speaker-labelled Segments and full_text/language."""
    audio = tmp_path / "call.wav"
    audio.write_bytes(b"fake-audio")
    segments = [
        {"start": 0.0, "end": 1.2, "text": " привет "},
        {"start": 1.2, "end": 2.5, "text": "как дела"},
    ]
    transcriber = GroqWhisperTranscriber(client=_fake_client(segments))  # type: ignore[arg-type]

    result = transcriber.transcribe(audio, speaker="agent")

    assert result.language == "ru"
    assert result.model == "groq/whisper-large-v3"
    assert result.full_text == "привет как дела"
    assert [s.speaker for s in result.segments] == ["agent", "agent"]
    assert result.segments[0].start == 0.0
    assert result.segments[1].text == "как дела"


def test_falls_back_to_text_without_segments(tmp_path: Path) -> None:
    """With no segments, full_text falls back to the response text."""
    audio = tmp_path / "call.wav"
    audio.write_bytes(b"fake-audio")
    transcriber = GroqWhisperTranscriber(client=_fake_client([]))  # type: ignore[arg-type]

    result = transcriber.transcribe(audio)

    assert result.full_text == "привет как дела"
    assert result.segments == []
