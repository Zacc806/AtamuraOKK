"""Tests for the meeting transcription engines (no network, no gRPC)."""

from __future__ import annotations

import pytest

from AtamuraOKK.scoring.meetings.config import config
from AtamuraOKK.scoring.meetings.transcribe import (
    OpenAITranscriber,
    build_transcriber,
)
from AtamuraOKK.scoring.meetings.yandex import YandexTranscriber, _assemble_text


def test_finals_become_lines() -> None:
    """Finals join with newlines (one utterance per line), blanks dropped.

    Regression for the truncation bug: a space-joined transcript has no line
    boundaries, so the chunker saw one giant line and long meetings were
    silently cut at the prompt cap.
    """
    finals = [" Здравствуйте, проходите. ", "  ", "Спасибо."]

    assert _assemble_text(finals) == "Здравствуйте, проходите.\nСпасибо."


def test_build_transcriber_defaults_to_yandex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The "yandex" engine builds the SpeechKit transcriber."""
    monkeypatch.setattr(config, "meetings_transcribe_engine", "yandex")
    monkeypatch.setattr(config, "yandex_sa_key_file", "authorized_key.json")

    assert isinstance(build_transcriber(), YandexTranscriber)


def test_build_transcriber_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """The "openai" engine stays selectable as the alternate."""
    monkeypatch.setattr(config, "meetings_transcribe_engine", "openai")

    assert isinstance(build_transcriber(), OpenAITranscriber)


def test_build_transcriber_rejects_removed_whisper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local whisper was removed (poor ru/kk quality); picking it is an error."""
    monkeypatch.setattr(config, "meetings_transcribe_engine", "whisper")

    with pytest.raises(ValueError, match="whisper"):
        build_transcriber()


def test_yandex_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an SA key file or API key the engine fails fast at build time."""
    monkeypatch.setattr(config, "yandex_sa_key_file", "")
    monkeypatch.setattr(config, "yandex_secret_key", "")

    with pytest.raises(RuntimeError, match="auth not configured"):
        YandexTranscriber()
