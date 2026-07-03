"""Unit tests for the appeal score recomputation helper."""

from __future__ import annotations

from AtamuraOKK.scoring.recompute import recompute_percent

# Legacy weighted payload (tm-call-v3 and earlier): varying max, no block_id.
_PER_CRITERION = [
    {"id": 1, "score": 2, "max": 5},
    {"id": 2, "score": 3, "max": 5},
]

# Binary flat payload (tm-call-v4): every max == 1. 3 ДА of 4 applicable -> 75%.
_BINARY = [
    {"id": 1, "score": 1, "max": 1, "block_id": "a"},
    {"id": 2, "score": 0, "max": 1, "block_id": "a"},
    {"id": 3, "score": 1, "max": 1, "block_id": "b"},
    {"id": 4, "score": 1, "max": 1, "block_id": "b"},
]


def test_empty_confirmed_returns_original_percent() -> None:
    """No confirmed criteria → the LLM percent (5/10 = 50%)."""
    assert recompute_percent(_PER_CRITERION, set()) == 50.0


def test_binary_flat_unchanged() -> None:
    """Binary payload with no confirmations → ДА ÷ applicable = 3/4 = 75%."""
    assert recompute_percent(_BINARY, set()) == 75.0


def test_binary_confirm_awards_full_mark() -> None:
    """Confirming id 2 → 4/4 = 100%."""
    assert recompute_percent(_BINARY, {2}) == 100.0


def test_binary_flat_ignores_blocks() -> None:
    """Flat: every element weighs the same, regardless of which block it is in."""
    payload = [
        {"id": 1, "score": 1, "max": 1, "block_id": "a"},
        {"id": 2, "score": 0, "max": 1, "block_id": "a"},
        {"id": 3, "score": 0, "max": 1, "block_id": "a"},
        {"id": 4, "score": 1, "max": 1, "block_id": "b"},
    ]
    assert recompute_percent(payload, set()) == 50.0  # 2/4
    assert recompute_percent(payload, {2}) == 75.0  # 3/4


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
