"""Filesystem layout for the Auphonic A/B spike (all under ``.auphonic_ab/``)."""

from __future__ import annotations

from pathlib import Path

WORK_DIR = Path(".auphonic_ab")
AUDIO_DIR = WORK_DIR / "audio"  # <id>.orig.mp3 / <id>.clean.mp3
TRANSCRIPT_DIR = WORK_DIR / "transcripts"  # <id>.before.json / <id>.after.json
OUT_DIR = WORK_DIR / "out"  # human-readable A/B markdown + summary.csv
MANIFEST = WORK_DIR / "manifest.json"
RESULTS = WORK_DIR / "results.json"

# Selection window: May 2026 in the report timezone.
MONTH_START = (2026, 5, 1)
MONTH_END = (2026, 6, 1)
SAMPLE_SIZE = 50
# Of the sample, aim for this many previously-FAILED calls (cleanup may rescue
# audio the prod pipeline couldn't transcribe); rest split kk/ru.
FAILED_TARGET = 10
