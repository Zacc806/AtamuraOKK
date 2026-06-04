"""Tests for calibration agreement metrics."""

from __future__ import annotations

import pytest

from AtamuraOKK.calibration.metrics import (
    cohen_kappa,
    mae,
    pass_fail_confusion,
    pearson,
    rmse,
    spearman,
)


def test_mae_and_rmse() -> None:
    """MAE and RMSE match hand-computed values."""
    assert mae([1, 2, 3], [1, 2, 5]) == pytest.approx(2 / 3)
    assert rmse([1, 2, 3], [1, 2, 5]) == pytest.approx((4 / 3) ** 0.5)


def test_pearson_perfect_and_inverse() -> None:
    """Pearson is +1 for a positive linear relation, -1 for an inverse one."""
    assert pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert pearson([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)


def test_pearson_constant_series_is_zero() -> None:
    """A constant series has no correlation."""
    assert pearson([1, 1, 1], [1, 2, 3]) == 0.0


def test_spearman_monotonic_nonlinear() -> None:
    """Spearman is 1 for a monotonic (non-linear) relation."""
    assert spearman([1, 2, 3, 4], [1, 4, 9, 16]) == pytest.approx(1.0)


def test_cohen_kappa_perfect_and_chance() -> None:
    """Kappa is 1 for perfect agreement and 0 for chance-level agreement."""
    assert cohen_kappa([True, False, True, False], [True, False, True, False]) == 1.0
    assert cohen_kappa([True, True, False, False], [True, False, True, False]) == 0.0


def test_cohen_kappa_unanimous() -> None:
    """Unanimous identical ratings give kappa 1.0 (no chance disagreement)."""
    assert cohen_kappa([True, True, True], [True, True, True]) == 1.0


def test_pass_fail_confusion() -> None:
    """Confusion counts and derived rates are correct."""
    conf = pass_fail_confusion([True, True, False], [True, False, False])
    assert conf["tp"] == 1
    assert conf["fp"] == 1
    assert conf["tn"] == 1
    assert conf["fn"] == 0
    assert conf["accuracy"] == pytest.approx(2 / 3)
    assert conf["precision"] == pytest.approx(0.5)
    assert conf["recall"] == pytest.approx(1.0)


def test_length_mismatch_raises() -> None:
    """Mismatched series lengths raise ValueError."""
    with pytest.raises(ValueError, match="length mismatch"):
        mae([1, 2], [1])
