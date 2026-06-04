"""Tests for the audio-retention cleanup job."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from AtamuraOKK.settings import settings
from AtamuraOKK.workers.maintenance import run_cleanup_audio


def test_cleanup_deletes_old_keeps_new(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Files older than the retention window are deleted; recent ones kept."""
    monkeypatch.setattr(settings, "audio_dir", tmp_path)
    monkeypatch.setattr(settings, "audio_retention_days", 30)

    old = tmp_path / "2026" / "old.mp3"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"x")
    new = tmp_path / "new.mp3"
    new.write_bytes(b"y")

    now = time.time()
    old_ts = now - 40 * 86400
    os.utime(old, (old_ts, old_ts))

    deleted = run_cleanup_audio(now=now)

    assert deleted == 1
    assert not old.exists()
    assert new.exists()


def test_cleanup_missing_dir_returns_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing audio dir is a no-op, not an error."""
    monkeypatch.setattr(settings, "audio_dir", tmp_path / "nope")
    assert run_cleanup_audio() == 0
