"""Diarization shaping for the Yandex async provider.

Two regressions are covered: stereo recordings must interleave both channels into
a time-ordered dialogue (not two glued per-channel blobs), and mono recordings
must split by speaker-labeling windows — falling back to one undifferentiated
segment when no windows arrive, so a mono call never regresses.
"""

from __future__ import annotations

from types import SimpleNamespace

from AtamuraOKK.transcription.base import Segment
from AtamuraOKK.transcription.yandex_async_provider import (
    _ordered_dialogue,
    _segments_by_channel,
    _segments_by_speaker,
    _SpeakerWindow,
    _Utterance,
    _utterance_span,
)


def test_stereo_channels_interleave_by_time() -> None:
    """Finals grouped by channel must come out ordered by start time."""
    # Finals arrive grouped by channel (both ch0 finals, then the ch1 final);
    # the result must still be ordered by start time across channels.
    utterances = [
        _Utterance("0", 0, 1000, "Здравствуйте, Atamura"),
        _Utterance("0", 2100, 3000, "По вашей заявке"),
        _Utterance("1", 1100, 2000, "Да, слушаю"),
    ]
    segs = _segments_by_channel(utterances)

    assert [s.speaker for s in segs] == ["agent", "customer", "agent"]
    assert [s.start for s in segs] == [0.0, 1.1, 2.1]
    assert all(s.end > s.start for s in segs)  # real, non-zero timestamps


def test_consecutive_same_speaker_merge() -> None:
    """Adjacent same-speaker utterances collapse into one turn."""
    utterances = [
        _Utterance("0", 0, 500, "Алло"),
        _Utterance("0", 500, 700, "это Atamura"),
        _Utterance("1", 1000, 1500, "Да"),
    ]
    segs = _segments_by_channel(utterances)

    assert len(segs) == 2
    assert segs[0].speaker == "agent"
    assert segs[0].text == "Алло это Atamura"
    assert segs[0].end == 0.7
    assert segs[1].speaker == "customer"


def test_mono_attributed_by_speaker_windows() -> None:
    """Mono utterances are attributed to the speaker window they overlap."""
    windows = [
        _SpeakerWindow("1", 0, 1000),
        _SpeakerWindow("2", 1000, 2000),
        _SpeakerWindow("1", 2000, 3000),
    ]
    utterances = [
        _Utterance("0", 0, 950, "привет"),
        _Utterance("0", 1050, 1900, "да"),
        _Utterance("0", 2100, 2900, "хорошо"),
    ]
    segs = _segments_by_speaker(utterances, windows)

    assert [s.speaker for s in segs] == ["agent", "customer", "agent"]
    assert {s.speaker for s in segs} == {"agent", "customer"}


def test_mono_without_windows_falls_back_to_single_blob() -> None:
    """No speaker windows → one undifferentiated segment (today's behaviour)."""
    utterances = [
        _Utterance("0", 0, 950, "привет"),
        _Utterance("0", 1050, 1900, "да"),
    ]
    segs = _segments_by_speaker(utterances, [])

    assert len(segs) == 1
    assert segs[0].speaker == "unknown"
    assert segs[0].text == "привет да"


def test_utterance_span_falls_back_to_word_timestamps() -> None:
    """When an alternative lacks a span, derive it from its words."""
    alt = SimpleNamespace(
        start_time_ms=0,
        end_time_ms=0,
        words=[
            SimpleNamespace(start_time_ms=120, end_time_ms=400),
            SimpleNamespace(start_time_ms=400, end_time_ms=900),
        ],
    )
    assert _utterance_span(alt) == (120, 900)


def test_ordered_dialogue_drops_empty_segments() -> None:
    """Whitespace-only segments are removed before ordering."""
    segs = _ordered_dialogue(
        [
            Segment("agent", 1.0, 2.0, "  "),
            Segment("customer", 0.0, 1.0, "Алло"),
        ],
    )
    assert [s.speaker for s in segs] == ["customer"]
