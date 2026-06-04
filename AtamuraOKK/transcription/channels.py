"""Audio channel utilities for stereo calls (ffprobe/ffmpeg).

Ported from the Phase 0 spike. The single-channel extraction uses the ``pan``
filter — the legacy ``-map_channel`` option was removed in ffmpeg 7.0 and would
crash the batch (audit finding).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from loguru import logger

_TARGET_RATE = "16000"


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
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError) as exc:
        logger.warning("ffprobe failed on {p}: {e}", p=audio_path, e=exc)
        return 0
    return int(out.stdout.strip() or 0)


def split_channel(audio_path: Path, channel: int, dest: Path) -> Path:
    """Extract a single channel to a 16 kHz mono wav with ffmpeg.

    :param audio_path: source (stereo) audio file.
    :param channel: 0-based channel index to extract.
    :param dest: output wav path.
    :returns: ``dest``.
    """
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-y",
            "-i",
            str(audio_path),
            "-af",
            f"pan=mono|c0=c{channel}",
            "-ar",
            _TARGET_RATE,
            str(dest),
        ],
        capture_output=True,
        check=True,
    )
    return dest
