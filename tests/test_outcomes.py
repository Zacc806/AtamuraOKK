"""Tests for sale-outcome stage classification (ТЗ 3.4)."""

from __future__ import annotations

from AtamuraOKK.outcomes import classify_stage


def test_won_stage() -> None:
    """A Bitrix WON stage classifies as won."""
    assert classify_stage("C5:WON") == "won"


def test_lose_and_apology_stages() -> None:
    """LOSE and APOLOGY stages classify as lose."""
    assert classify_stage("C1:LOSE") == "lose"
    assert classify_stage("APOLOGY") == "lose"


def test_in_progress_is_pending() -> None:
    """Any other / empty stage is still pending."""
    assert classify_stage("C2:PREPARATION") == "pending"
    assert classify_stage("") == "pending"
