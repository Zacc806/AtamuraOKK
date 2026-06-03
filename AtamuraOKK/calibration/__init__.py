"""Calibration: compare AI scores against human OKK scores (go/no-go gate)."""

from AtamuraOKK.calibration.harness import CalibrationReport, compare
from AtamuraOKK.calibration.metrics import (
    cohen_kappa,
    mae,
    pass_fail_confusion,
    pearson,
    rmse,
    spearman,
)
from AtamuraOKK.calibration.xlsx_loader import HumanCall, load_human_calls

__all__ = [
    "CalibrationReport",
    "HumanCall",
    "cohen_kappa",
    "compare",
    "load_human_calls",
    "mae",
    "pass_fail_confusion",
    "pearson",
    "rmse",
    "spearman",
]
