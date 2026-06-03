"""Shared DB enums."""

from __future__ import annotations

import enum


class CallStatus(enum.StrEnum):
    """Lifecycle of a call row through the pipeline."""

    NEW = "NEW"  # ingested, recording not yet downloaded
    DOWNLOADED = "DOWNLOADED"  # audio fetched, awaiting transcription
    TRANSCRIBED = "TRANSCRIBED"  # transcript stored, awaiting scoring
    SCORED = "SCORED"  # score stored — terminal success
    FAILED = "FAILED"  # a stage errored (see error/failed_stage/attempts)
    SKIPPED = "SKIPPED"  # answered but unscoreable (too short / no recording)
