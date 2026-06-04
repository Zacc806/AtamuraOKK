"""Tests for audio channel utilities."""

from __future__ import annotations

from pathlib import Path

from AtamuraOKK.transcription.channels import probe_channels


def test_probe_channels_missing_tool_returns_zero() -> None:
    """probe_channels degrades to 0 when ffprobe is absent or the file is bad."""
    assert probe_channels(Path("does-not-exist.mp3")) == 0
