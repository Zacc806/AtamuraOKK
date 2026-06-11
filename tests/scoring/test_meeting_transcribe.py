"""Tests for the meeting transcriber's text assembly (no model download)."""

from __future__ import annotations

from pathlib import Path

from AtamuraOKK.scoring.meetings.transcribe import WhisperTranscriber


class _Seg:
    def __init__(self, text: str) -> None:
        self.text = text


class _Info:
    language = "ru"


class _StubModel:
    """Stands in for the loaded faster-whisper model."""

    def transcribe(self, path: str, **kwargs: object) -> tuple[list[_Seg], _Info]:
        return (
            [_Seg(" Здравствуйте, проходите. "), _Seg("  "), _Seg("Спасибо.")],
            _Info(),
        )


def test_whisper_segments_become_lines() -> None:
    """Segments join with newlines (one utterance per line), blanks dropped.

    Regression for the truncation bug: a space-joined transcript has no line
    boundaries, so the chunker saw one giant line and long meetings were
    silently cut at the prompt cap.
    """
    transcriber = WhisperTranscriber(model="tiny")
    transcriber._model = _StubModel()  # noqa: SLF001

    out = transcriber._transcribe_sync(Path("ignored.wav"))  # noqa: SLF001

    assert out.text == "Здравствуйте, проходите.\nСпасибо."
    assert out.language == "ru"
