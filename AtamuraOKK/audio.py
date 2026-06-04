"""ffmpeg-based audio helpers shared by the pipeline.

Telephony recordings here are stereo MP3 (agent on one channel, customer on the
other). We probe channel count and split each channel to a 16 kHz mono WAV for
per-speaker transcription.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from loguru import logger


def probe_channels(audio_path: Path) -> int:
    """Return the audio channel count via ffprobe (0 if undetermined)."""
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


def extract_channel(audio_path: Path, channel: int, dest: Path) -> Path:
    """Extract one channel to a 16 kHz mono WAV (``pan`` filter; ffmpeg 7+)."""
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-y",
            "-i",
            str(audio_path),
            "-af",
            f"pan=mono|c0=c{channel}",
            "-ar",
            "16000",
            str(dest),
        ],
        capture_output=True,
        check=True,
    )
    return dest


def to_mono_wav(audio_path: Path, dest: Path) -> Path:
    """Downmix any input to a single 16 kHz mono WAV."""
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-y",
            "-i",
            str(audio_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(dest),
        ],
        capture_output=True,
        check=True,
    )
    return dest
