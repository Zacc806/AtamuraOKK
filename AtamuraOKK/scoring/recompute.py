"""Recompute a call's QA percent after an appeal confirms criteria.

When a head confirms a per-criterion appeal, each confirmed criterion is awarded
full marks; the corrected total percent is derived from the stored per-criterion
payload (``Score.criteria["per_criterion"]``). This mirrors the percent formula
in :func:`AtamuraOKK.scoring.worker._assemble` but operates on the already-stored
breakdown, so the web layer can recompute without re-running the scorer.

Kept in its own module so the read/appeal layer imports it without pulling in the
scoring worker (and its heavy deps).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def recompute_percent(
    per_criterion: Iterable[Mapping[str, Any]],
    confirmed_ids: set[int],
) -> float:
    """Corrected 0-100 percent: confirmed criteria get full marks, rest unchanged.

    ``per_criterion`` is the list of ``{"id", "score", "max", ...}`` entries the
    scorer persisted (see ``worker._assemble``). A criterion whose ``id`` is in
    ``confirmed_ids`` contributes its ``max`` to the numerator; every criterion
    contributes its ``max`` to the denominator, exactly as the original scoring
    did — so the denominator is unchanged and a confirmation can only raise the
    percent. Returns 0.0 when there are no scorable points.
    """
    raw = 0
    max_points = 0
    for crit in per_criterion:
        crit_max = int(crit["max"])
        max_points += crit_max
        if int(crit["id"]) in confirmed_ids:
            raw += crit_max
        else:
            raw += int(crit["score"])
    return round(100.0 * raw / max_points, 2) if max_points else 0.0
