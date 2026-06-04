"""Agreement metrics for AI-vs-human calibration. Pure, dependency-free."""

from __future__ import annotations

import math
from collections.abc import Sequence


def _check(a: Sequence[float], b: Sequence[float]) -> None:
    if len(a) != len(b):
        msg = f"length mismatch: {len(a)} != {len(b)}"
        raise ValueError(msg)


def mae(pred: Sequence[float], truth: Sequence[float]) -> float:
    """Mean absolute error."""
    _check(pred, truth)
    if not pred:
        return 0.0
    return sum(abs(p - t) for p, t in zip(pred, truth, strict=True)) / len(pred)


def rmse(pred: Sequence[float], truth: Sequence[float]) -> float:
    """Root mean squared error."""
    _check(pred, truth)
    if not pred:
        return 0.0
    mean_sq = sum((p - t) ** 2 for p, t in zip(pred, truth, strict=True)) / len(pred)
    return math.sqrt(mean_sq)


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson correlation; 0.0 if either series is constant or empty."""
    _check(xs, ys)
    n = len(xs)
    if n == 0:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return 0.0
    return cov / (sx * sy)


def _rankdata(values: Sequence[float]) -> list[float]:
    """Average ranks (1-based), ties averaged."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1  # average of 1-based positions i+1..j+1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Spearman rank correlation (Pearson on ranks)."""
    _check(xs, ys)
    if not xs:
        return 0.0
    return pearson(_rankdata(xs), _rankdata(ys))


def cohen_kappa(a: Sequence[bool], b: Sequence[bool]) -> float:
    """Cohen's kappa for two binary raters (chance-corrected agreement)."""
    _check(a, b)
    n = len(a)
    if n == 0:
        return 0.0
    po = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    pa = sum(a) / n
    pb = sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    if pe >= 1.0:
        return 1.0 if po >= 1.0 else 0.0
    return (po - pe) / (1 - pe)


def pass_fail_confusion(
    pred: Sequence[bool],
    truth: Sequence[bool],
) -> dict[str, float]:
    """Confusion matrix + accuracy/precision/recall/kappa for a pass/fail decision."""
    _check(pred, truth)
    tp = sum(1 for p, t in zip(pred, truth, strict=True) if p and t)
    fp = sum(1 for p, t in zip(pred, truth, strict=True) if p and not t)
    tn = sum(1 for p, t in zip(pred, truth, strict=True) if not p and not t)
    fn = sum(1 for p, t in zip(pred, truth, strict=True) if not p and t)
    n = len(pred)
    accuracy = (tp + tn) / n if n else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return {
        "tp": float(tp),
        "fp": float(fp),
        "tn": float(tn),
        "fn": float(fn),
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "kappa": cohen_kappa(pred, truth),
    }
