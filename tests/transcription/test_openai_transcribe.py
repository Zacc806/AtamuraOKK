"""Tests for the OpenAI transcriber (fake injected client, no network/SDK)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from AtamuraOKK.transcription.openai_transcribe import OpenAITranscriber


@dataclass
class _FakeResponse:
    """Mimics the OpenAI transcription response object (only ``.text``)."""

    text: str


class _FakeTranscriptions:
    def __init__(self, text: str) -> None:
        self._text = text
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> _FakeResponse:
        """Record the call kwargs and return a canned transcription."""
        self.kwargs = kwargs
        return _FakeResponse(text=self._text)


class _FakeAudio:
    def __init__(self, text: str) -> None:
        self.transcriptions = _FakeTranscriptions(text)


class _FakeClient:
    """Stand-in for ``openai.OpenAI`` exposing only ``audio.transcriptions``."""

    def __init__(self, text: str) -> None:
        self.audio = _FakeAudio(text)


def test_russian_text_detected_as_ru(tmp_path: Path) -> None:
    """Plain Russian transcription is labelled ru and tagged with the model."""
    audio = tmp_path / "call.mp3"
    audio.write_bytes(b"fake-audio")
    client = _FakeClient("здравствуйте, расскажите про квартиру")

    result = OpenAITranscriber(model="gpt-4o-transcribe", client=client).transcribe(
        audio,
    )

    assert result.language == "ru"
    assert result.full_text == "здравствуйте, расскажите про квартиру"
    assert result.model == "openai/gpt-4o-transcribe"
    assert result.segments == []


def test_kazakh_signal_detected_as_kk(tmp_path: Path) -> None:
    """Kazakh-specific letters in the text flip the detected language to kk."""
    audio = tmp_path / "call.mp3"
    audio.write_bytes(b"fake-audio")
    client = _FakeClient("сәлеметсіз бе, әңгімелесейік")

    result = OpenAITranscriber(client=client).transcribe(audio)

    assert result.language == "kk"


def test_explicit_language_is_passed_through(tmp_path: Path) -> None:
    """An explicit language overrides detection and reaches the API call."""
    audio = tmp_path / "call.mp3"
    audio.write_bytes(b"fake-audio")
    client = _FakeClient("привет")

    result = OpenAITranscriber(client=client).transcribe(audio, language="ru")

    assert result.language == "ru"
    assert client.audio.transcriptions.kwargs["language"] == "ru"
