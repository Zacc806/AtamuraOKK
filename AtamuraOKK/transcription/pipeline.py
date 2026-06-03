"""Transcribe a downloaded recording, handling stereo vs mono.

Stereo native calls are split per channel (ch0=agent, ch1=customer) and merged
into one timestamp-ordered, speaker-labelled transcript — the no-diarization
path. Mono calls are transcribed whole with an ``unknown`` speaker. Ported from
the Phase 0 spike ``transcribe_call``.
"""

from __future__ import annotations

from pathlib import Path

from AtamuraOKK.transcription.base import Segment, Transcriber, TranscriptResult
from AtamuraOKK.transcription.channels import split_channel


def transcribe_file(
    transcriber: Transcriber,
    audio_path: Path,
    *,
    is_stereo: bool,
) -> TranscriptResult:
    """Transcribe one recording (synchronous; run via ``asyncio.to_thread``)."""
    if not is_stereo:
        result = transcriber.transcribe(audio_path, speaker="unknown")
        result.meta["stereo"] = False
        return result

    work = audio_path.parent / "_channels"
    work.mkdir(exist_ok=True)
    stem = audio_path.stem
    agent = transcriber.transcribe(
        split_channel(audio_path, 0, work / f"{stem}_agent.wav"),
        speaker="agent",
    )
    customer = transcriber.transcribe(
        split_channel(audio_path, 1, work / f"{stem}_customer.wav"),
        speaker="customer",
    )
    merged: list[Segment] = sorted(
        agent.segments + customer.segments,
        key=lambda s: s.start,
    )
    full_text = "\n".join(f"[{s.speaker}] {s.text}" for s in merged)
    # Trust the longer channel's detected language (usually the customer).
    language = (
        customer.language
        if len(customer.full_text) > len(agent.full_text)
        else agent.language
    )
    return TranscriptResult(
        language=language,
        full_text=full_text,
        segments=merged,
        model=agent.model,
        meta={"channels": 2, "stereo": True},
    )
