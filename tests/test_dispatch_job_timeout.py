"""C1 regression: arq job_timeout must be shorter than the stale-claim TTL.

If ``job_timeout >= claim_stale_seconds_*`` the reconciler reverts and re-enqueues
a long-but-alive job while it is still running, so the (paid) stage runs twice.
"""

from __future__ import annotations

from AtamuraOKK.dispatch import worker_settings as ws
from AtamuraOKK.dispatch.worker_settings import _job_timeout
from AtamuraOKK.settings import settings


def test_each_stage_job_timeout_is_below_its_stale_ttl() -> None:
    """Every stage's job_timeout sits under its reconciler TTL (and stays positive)."""
    pairs = [
        (ws.DownloadWorker.job_timeout, settings.claim_stale_seconds_download),
        (ws.TranscribeWorker.job_timeout, settings.claim_stale_seconds_transcribe),
        (ws.ScoreWorker.job_timeout, settings.claim_stale_seconds_score),
    ]
    for job_timeout, ttl in pairs:
        assert job_timeout < ttl
        assert job_timeout >= 60


def test_job_timeout_subtracts_margin() -> None:
    """A comfortable TTL yields TTL minus the configured margin."""
    assert _job_timeout(1800) == 1800 - settings.claim_job_timeout_margin_seconds


def test_job_timeout_floors_at_60() -> None:
    """A TTL smaller than the margin must not produce a non-positive timeout."""
    assert _job_timeout(settings.claim_job_timeout_margin_seconds - 10) == 60
