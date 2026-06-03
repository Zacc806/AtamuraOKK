"""Assemble a validated :class:`LLMScore` into a final :class:`ScoreResult`.

This is the deterministic, provider-independent step: merge LLM-scored criteria
with the rubric's auto_check criteria, clamp to valid ranges, compute totals.
Pure and fully unit-testable without any network.
"""

from __future__ import annotations

from typing import Any

from AtamuraOKK.scoring.base import CallForScoring, CriterionScore, ScoreResult
from AtamuraOKK.scoring.rubric import Rubric
from AtamuraOKK.scoring.schema import LLMScore


def assemble_score(
    llm: LLMScore,
    *,
    rubric: Rubric,
    call: CallForScoring,
    language: str,
    provider: str,
    model: str,
    pass_threshold: int,
    meta: dict[str, Any] | None = None,
) -> ScoreResult:
    """Merge LLM + auto_check scores into a :class:`ScoreResult`.

    LLM-provided scores are clamped to ``[0, max_score]``; criteria the LLM
    omitted are recorded in ``meta["missing_criteria"]`` and scored 0.
    """
    auto = rubric.auto_scores(duration_sec=call.duration_sec)
    criteria: list[CriterionScore] = []
    missing: list[int] = []
    clamped = 0

    for crit in rubric.criteria:
        if crit.id in auto:
            criteria.append(
                CriterionScore(
                    id=crit.id,
                    block=crit.block,
                    name=crit.name,
                    score=auto[crit.id],
                    max_score=crit.max_score,
                    auto=True,
                ),
            )
            continue

        raw = llm.scores.get(str(crit.id))
        if raw is None:
            missing.append(crit.id)
            points = 0
        else:
            points = int(raw)
        if points < 0 or points > crit.max_score:
            clamped += 1
            points = max(0, min(points, crit.max_score))
        criteria.append(
            CriterionScore(
                id=crit.id,
                block=crit.block,
                name=crit.name,
                score=points,
                max_score=crit.max_score,
                auto=False,
            ),
        )

    total = sum(cs.score for cs in criteria)
    score_pct = round(total / rubric.max_total_score * 100, 1)

    out_meta: dict[str, Any] = dict(meta or {})
    if missing:
        out_meta["missing_criteria"] = missing
    if clamped:
        out_meta["clamped_criteria"] = clamped

    return ScoreResult(
        rubric_version=rubric.id,
        total_score=total,
        max_total=rubric.max_total_score,
        score_pct=score_pct,
        passed=score_pct >= pass_threshold,
        criteria=criteria,
        client_agreed_meeting=llm.client_agreed_meeting,
        manager_tone=llm.manager_tone,
        red_flags=list(llm.red_flags_found),
        summary=llm.summary,
        language=language,
        provider=provider,
        model=model,
        needs_human_review=len(missing) >= 3,
        meta=out_meta,
    )
