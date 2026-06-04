"""Tests for the Yandex SpeechKit transcriber (httpx MockTransport)."""

from __future__ import annotations

from pathlib import Path

import httpx

from AtamuraOKK.transcription.yandex_speech import YandexSpeechKitTranscriber


def test_recognizes_kazakh(tmp_path: Path) -> None:
    """A 200 SpeechKit response maps to a kk TranscriptResult."""
    audio = tmp_path / "call.wav"
    audio.write_bytes(b"fake-audio")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "сәлеметсіз бе"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transcriber = YandexSpeechKitTranscriber(client=client)

    result = transcriber.transcribe(audio, speaker="agent")

    assert result.language == "kk"
    assert result.full_text == "сәлеметсіз бе"
    assert result.model == "yandex/speechkit"
    assert result.segments[0].speaker == "agent"


def test_empty_result_has_no_segments(tmp_path: Path) -> None:
    """An empty recognition result yields no segments."""
    audio = tmp_path / "call.wav"
    audio.write_bytes(b"fake-audio")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": ""})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = YandexSpeechKitTranscriber(client=client).transcribe(audio)

    assert result.full_text == ""
    assert result.segments == []
