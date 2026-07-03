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

    ``per_criterion`` is the list of ``{"id", "score", "max", "block_id", ...}``
    entries the scorer persisted (see ``worker._assemble``). A criterion in
    ``confirmed_ids`` is awarded full marks; the denominator is unchanged, so a
    confirmation can only raise the percent.

    Two scoring models are supported, detected from the payload so appeals on both
    historical (weighted) and current (binary) scores recompute correctly:

    - **binary flat** (``tm-call-v4``+): every entry has ``max == 1``. The corrected
      percent is ``ДА ÷ applicable × 100`` over all applicable elements (each weighs
      the same) — mirroring ``_assemble``; ``per_criterion`` already excludes Н.П.
      elements, so its length is the applicable count.
    - **legacy weighted sum** (``tm-call-v3`` and earlier): the corrected percent
      is ``Σ score ÷ Σ max × 100`` over all criteria.

    Returns 0.0 when there are no scorable points.
    """
    items = list(per_criterion)
    if not items:
        return 0.0

    binary = all(int(c.get("max", 0)) == 1 for c in items)
    if binary:
        yes = sum(
            1 if int(c["id"]) in confirmed_ids else min(int(c["score"]), 1)
            for c in items
        )
        return round(100.0 * yes / len(items), 2)

    raw = 0
    max_points = 0
    for crit in items:
        crit_max = int(crit["max"])
        max_points += crit_max
        if int(crit["id"]) in confirmed_ids:
            raw += crit_max
        else:
            raw += int(crit["score"])
    return round(100.0 * raw / max_points, 2) if max_points else 0.0
