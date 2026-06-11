"""ffmpeg/ffprobe helpers for meeting recordings — self-contained.

Meeting recordings are single-mic mono (WhatsApp voice notes, phone dictaphones),
so unlike the stereo telephony path there is no agent/customer channel to split:
we just downmix to a 16 kHz mono WAV for the STT engine and probe duration.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from loguru import logger


def probe_duration_sec(media_path: Path) -> int:
    """Audio duration in whole seconds via ffprobe (0 if undetermined)."""
    try:
        out = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(media_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return int(float(out.stdout.strip() or 0))
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError) as exc:
        logger.warning("ffprobe failed on {p}: {e}", p=media_path, e=exc)
        return 0


def to_mono_wav(media_path: Path, dest: Path) -> Path:
    """Downmix any audio/video input to a single 16 kHz mono WAV."""
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-y",
            "-i",
            str(media_path),
            "-vn",
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
