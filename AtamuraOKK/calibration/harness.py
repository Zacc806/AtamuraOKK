"""Pair AI scores against human scores by CRM deal id and judge agreement.

The go/no-go gate for the whole project: does the AI scorer agree with the human
OKK graders? The actual scorer run (producing the ``ai`` map) needs provider keys
and transcripts of the scored meetings; this module is the pure comparison +
verdict, fully testable with synthetic inputs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from AtamuraOKK.calibration.metrics import (
    cohen_kappa,
    mae,
    pass_fail_confusion,
    pearson,
    rmse,
    spearman,
)
from AtamuraOKK.calibration.xlsx_loader import HumanCall
from AtamuraOKK.scoring.base import ScoreResult

# Default go/no-go thresholds (overridable).
DEFAULT_GATES: dict[str, float] = {
    "passfail_kappa_min": 0.6,
    "total_mae_max": 7.0,
    "spearman_min": 0.7,
}


@dataclass(slots=True)
class CalibrationReport:
    """Agreement between AI and human scores over the matched calls."""

    n: int
    total_mae: float
    total_rmse: float
    pearson: float
    spearman: float
    passfail: dict[str, float]
    per_criterion_kappa: dict[int, float]
    gates: dict[str, bool]
    verdict: str  # "PASS" | "REVISE" | "FAIL"

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form."""
        return asdict(self)


def _ai_criterion_yes(result: ScoreResult, criterion_id: int) -> bool:
    return any(c.id == criterion_id and c.score > 0 for c in result.criteria)


def compare(
    human: Sequence[HumanCall],
    ai: Mapping[int, ScoreResult],
    *,
    max_total: int,
    pass_threshold: int,
    gates: Mapping[str, float] = DEFAULT_GATES,
) -> CalibrationReport:
    """Compare AI vs human scores over calls present in both (joined by deal id).

    :param human: human-scored calls (from the xlsx loader).
    :param ai: AI :class:`ScoreResult` keyed by CRM deal id.
    :param max_total: rubric raw maximum (e.g. 50), to normalize human totals.
    :param pass_threshold: pass cutoff on the 0-100 scale (e.g. 75).
    :param gates: go/no-go thresholds.
    :returns: the calibration report with a PASS/REVISE/FAIL verdict.
    """
    matched = [
        (h, ai[h.crm_deal_id])
        for h in human
        if h.crm_deal_id is not None
        and h.crm_deal_id in ai
        and h.raw_total is not None
    ]
    if not matched:
        return CalibrationReport(
            n=0,
            total_mae=0.0,
            total_rmse=0.0,
            pearson=0.0,
            spearman=0.0,
            passfail={},
            per_criterion_kappa={},
            gates=dict.fromkeys(gates, False),
            verdict="FAIL",
        )

    human_pct = [(h.raw_total or 0) / max_total * 100 for h, _ in matched]
    # Calibrate on the bonus-free base score: the human xlsx carries no КЭВ
    # bonus, so comparing it against the +10-inflated score_pct/passed would be a
    # guaranteed scale mismatch. base_score_pct is stored in meta by both scoring
    # paths; fall back to score_pct for results that predate it.
    ai_pct = [
        float(a.meta.get("base_score_pct", a.score_pct)) for _, a in matched
    ]
    human_pass = [p >= pass_threshold for p in human_pct]
    ai_pass = [p >= pass_threshold for p in ai_pct]

    criterion_ids = sorted(
        {cid for h, _ in matched for cid in h.per_criterion},
    )
    per_criterion_kappa = {
        cid: cohen_kappa(
            [h.per_criterion.get(cid, 0) > 0 for h, _ in matched],
            [_ai_criterion_yes(a, cid) for _, a in matched],
        )
        for cid in criterion_ids
    }

    passfail = pass_fail_confusion(ai_pass, human_pass)
    report_mae = mae(ai_pct, human_pct)
    report_spearman = spearman(ai_pct, human_pct)

    gate_results = {
        "passfail_kappa_min": passfail["kappa"] >= gates["passfail_kappa_min"],
        "total_mae_max": report_mae <= gates["total_mae_max"],
        "spearman_min": report_spearman >= gates["spearman_min"],
    }
    passed = sum(gate_results.values())
    if passed == len(gate_results):
        verdict = "PASS"
    elif passed == 0:
        verdict = "FAIL"
    else:
        verdict = "REVISE"

    return CalibrationReport(
        n=len(matched),
        total_mae=report_mae,
        total_rmse=rmse(ai_pct, human_pct),
        pearson=pearson(ai_pct, human_pct),
        spearman=report_spearman,
        passfail=passfail,
        per_criterion_kappa=per_criterion_kappa,
        gates=gate_results,
        verdict=verdict,
    )
