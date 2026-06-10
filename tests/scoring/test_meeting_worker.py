"""Tests for the ОП meeting scheduler worker (job guards + job registration)."""

from __future__ import annotations

from typing import Any

from AtamuraOKK.scoring.meetings import worker
from AtamuraOKK.scoring.meetings.worker import (
    _build_scheduler,
    _job_pipeline,
    _job_retry,
)


async def test_job_pipeline_swallows_errors(monkeypatch: Any) -> None:
    """A failing pipeline pass is logged, not raised, so the scheduler survives."""

    async def _boom() -> None:
        raise RuntimeError("pipeline blew up")

    monkeypatch.setattr(worker, "run_pipeline", _boom)
    await _job_pipeline()  # must not raise


async def test_job_retry_swallows_errors(monkeypatch: Any) -> None:
    """A failing retry pass is contained too."""

    async def _boom() -> None:
        raise RuntimeError("retry blew up")

    monkeypatch.setattr(worker, "requeue_failed", _boom)
    await _job_retry()  # must not raise


def test_build_scheduler_registers_both_jobs() -> None:
    """The scheduler wires exactly the pipeline + retry jobs."""
    scheduler = _build_scheduler()
    assert {j.id for j in scheduler.get_jobs()} == {
        "meetings-pipeline",
        "meetings-retry",
    }
