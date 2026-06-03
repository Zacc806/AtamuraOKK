"""Stage 3: transcribe each downloaded recording with faster-whisper.

Probes channel count with ffprobe. If the recording is **stereo** we split it
and transcribe each channel separately (channel 0 = agent, channel 1 =
customer by convention), then merge into one timestamp-ordered transcript —
this is the no-diarization path the plan prefers. If **mono**, we transcribe
the whole file with a single ``unknown`` speaker (diarization is deferred to
Phase 2; it is not needed to measure WER).

Requires the ``spike`` dependency group and the ``ffmpeg``/``ffprobe`` binaries.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from loguru import logger

from AtamuraOKK.settings import settings
from AtamuraOKK.transcription.base import Segment, TranscriptResult
from AtamuraOKK.transcription.whisper import FasterWhisperTranscriber


def probe_channels(audio_path: Path) -> int:
    """Return the number of audio channels via ffprobe (0 if undetermined)."""
    try:
        out = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=channels",
                "-of",
                "default=nw=1:nk=1",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return int(out.stdout.strip() or 0)
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError) as exc:
        logger.warning("ffprobe failed on {p}: {e}", p=audio_path, e=exc)
        return 0


def _split_channel(audio_path: Path, channel: int, dest: Path) -> Path:
    """Extract a single channel to a mono wav with ffmpeg."""
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-y",
            "-i",
            str(audio_path),
            "-map_channel",
            f"0.0.{channel}",
            "-ar",
            "16000",
            str(dest),
        ],
        capture_output=True,
        check=True,
    )
    return dest


def transcribe_call(
    transcriber: FasterWhisperTranscriber,
    audio_path: Path,
) -> TranscriptResult:
    """Transcribe one recording, handling stereo vs mono."""
    channels = probe_channels(audio_path)
    if channels >= 2:
        work = audio_path.parent / "_channels"
        work.mkdir(exist_ok=True)
        stem = audio_path.stem
        agent = transcriber.transcribe(
            _split_channel(audio_path, 0, work / f"{stem}_agent.wav"),
            speaker="agent",
        )
        customer = transcriber.transcribe(
            _split_channel(audio_path, 1, work / f"{stem}_customer.wav"),
            speaker="customer",
        )
        merged: list[Segment] = sorted(
            agent.segments + customer.segments,
            key=lambda s: s.start,
        )
        full_text = "\n".join(f"[{s.speaker}] {s.text}" for s in merged)
        # Detected language: trust the longer channel (usually the customer).
        language = (
            customer.language
            if len(customer.full_text)
            > len(
                agent.full_text,
            )
            else agent.language
        )
        return TranscriptResult(
            language=language,
            full_text=full_text,
            segments=merged,
            model=agent.model,
            meta={"channels": channels, "stereo": True},
        )

    result = transcriber.transcribe(audio_path, speaker="unknown")
    result.meta["channels"] = channels
    result.meta["stereo"] = False
    return result


def transcribe_all() -> list[dict[str, Any]]:
    """Transcribe every downloaded call; write ``<spike_dir>/transcripts/``."""
    calls_path = settings.spike_dir / "calls.json"
    calls: list[dict[str, Any]] = json.loads(calls_path.read_text(encoding="utf-8"))
    out_dir = settings.spike_dir / "transcripts"
    out_dir.mkdir(parents=True, exist_ok=True)

    transcriber = FasterWhisperTranscriber()
    done = 0
    for call in calls:
        audio_path = call.get("audio_path")
        if not audio_path or not Path(audio_path).exists():
            continue
        call_id = call["CALL_ID"]
        result = transcribe_call(transcriber, Path(audio_path))
        payload = {
            "call_id": call_id,
            "language": result.language,
            "full_text": result.full_text,
            "segments": [asdict(s) for s in result.segments],
            "model": result.model,
            "meta": result.meta,
        }
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in call_id)
        (out_dir / f"{safe}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        done += 1
        logger.info(
            "Transcribed {id} ({lang}, {ch}ch)",
            id=call_id,
            lang=result.language,
            ch=result.meta.get("channels"),
        )

    logger.info("Transcribed {n} recordings to {d}", n=done, d=out_dir)
    return calls
