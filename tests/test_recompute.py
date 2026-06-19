"""Unit tests for the appeal score recomputation helper."""

from __future__ import annotations

from AtamuraOKK.scoring.recompute import recompute_percent

_PER_CRITERION = [
    {"id": 1, "score": 2, "max": 5},
    {"id": 2, "score": 3, "max": 5},
]


def test_empty_confirmed_returns_original_percent() -> None:
    """No confirmed criteria → the LLM percent (5/10 = 50%)."""
    assert recompute_percent(_PER_CRITERION, set()) == 50.0


def test_confirming_subset_awards_full_marks() -> None:
    """Confirming criterion 1 → (5 + 3) / 10 = 80%."""
    assert recompute_percent(_PER_CRITERION, {1}) == 80.0


def test_confirming_all_reaches_100() -> None:
    """Confirming every criterion → full marks."""
    assert recompute_percent(_PER_CRITERION, {1, 2}) == 100.0


def test_unknown_confirmed_id_is_ignored() -> None:
    """A confirmed id not in the payload can't change the score."""
    assert recompute_percent(_PER_CRITERION, {99}) == 50.0


def test_no_points_is_zero() -> None:
    """An empty / zero-max payload yields 0.0 rather than dividing by zero."""
    assert recompute_percent([], {1}) == 0.0
