"""Tests for language-routed transcription (faster-whisper -> Yandex)."""

from __future__ import annotations

from pathlib import Path

from AtamuraOKK.transcription.base import TranscriptResult
from AtamuraOKK.transcription.router import LanguageRoutedTranscriber


class _Stub:
    """A transcriber stub returning a fixed result and recording the call."""

    def __init__(self, result: TranscriptResult) -> None:
        self._result = result
        self.called = False

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        speaker: str = "unknown",
    ) -> TranscriptResult:
        """Record the call and return the preset result."""
        self.called = True
        return self._result


def _tr(language: str, text: str = "", prob: float = 1.0) -> TranscriptResult:
    """Build a TranscriptResult with a detected language + probability."""
    return TranscriptResult(
        language=language,
        full_text=text,
        segments=[],
        model="m",
        meta={"language_probability": prob},
    )


def _route(primary: _Stub, kazakh: _Stub) -> TranscriptResult:
    router = LanguageRoutedTranscriber(primary=primary, kazakh=kazakh)
    return router.transcribe(Path("call.wav"))


def test_russian_stays_on_primary() -> None:
    """Confident Russian keeps the free faster-whisper result."""
    primary = _Stub(_tr("ru", "привет как дела", 0.95))
    kazakh = _Stub(_tr("kk", "x"))
    result = _route(primary, kazakh)
    assert primary.called
    assert not kazakh.called
    assert result.language == "ru"


def test_detected_kazakh_escalates() -> None:
    """Detected Kazakh is re-transcribed on Yandex."""
    primary = _Stub(_tr("kk", "x", 0.9))
    kazakh = _Stub(_tr("kk", "сәлем"))
    result = _route(primary, kazakh)
    assert primary.called
    assert kazakh.called
    assert result.full_text == "сәлем"


def test_kazakh_signal_in_russian_escalates() -> None:
    """A Kazakh token in 'Russian' text triggers escalation."""
    primary = _Stub(_tr("ru", "привет менеджер керек ма", 0.95))
    kazakh = _Stub(_tr("kk", "сәлем"))
    _route(primary, kazakh)
    assert kazakh.called


def test_low_confidence_russian_escalates() -> None:
    """Low-confidence Russian escalates to the Kazakh path."""
    primary = _Stub(_tr("ru", "привет", 0.4))
    kazakh = _Stub(_tr("kk", "сәлем"))
    _route(primary, kazakh)
    assert kazakh.called
