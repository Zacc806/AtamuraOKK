"""Tests for the map-reduce MeetingScorer (fake per-chunk scorer, no network)."""

from __future__ import annotations

from AtamuraOKK.scoring.base import CallForScoring, CriterionScore, ScoreResult
from AtamuraOKK.scoring.chunking import chunk_transcript
from AtamuraOKK.scoring.meeting import MeetingScorer
from AtamuraOKK.scoring.rubric import load_rubric

RUBRIC = load_rubric("okk_meeting_v1")


def _result(
    scores: dict[int, int],
    *,
    client_agreed: bool = False,
    red_flags: list[str] | None = None,
    tone: str = "нейтральный",
    summary: str = "",
    adherence: float | None = None,
    deviations: list[str] | None = None,
    needs_review: bool = False,
) -> ScoreResult:
    """Build an okk_meeting_v1 ScoreResult with the given per-criterion scores."""
    criteria = [
        CriterionScore(
            id=c.id,
            block=c.block,
            name=c.name,
            score=scores.get(c.id, 0),
            max_score=c.max_score,
        )
        for c in RUBRIC.criteria
    ]
    total = sum(cs.score for cs in criteria)
    return ScoreResult(
        rubric_version=RUBRIC.id,
        total_score=total,
        max_total=RUBRIC.max_total_score,
        score_pct=round(total / RUBRIC.max_total_score * 100, 1),
        passed=False,
        criteria=criteria,
        call_type="первичный",
        client_agreed_meeting=client_agreed,
        manager_tone=tone,
        red_flags=red_flags or [],
        summary=summary,
        language="ru",
        provider="anthropic",
        model="test",
        needs_human_review=needs_review,
        script_adherence=adherence,
        script_deviations=deviations or [],
    )


class _FakeScorer:
    """Returns a preset result per chunk index; records every call_ref seen."""

    def __init__(
        self,
        by_index: list[ScoreResult],
        *,
        single: ScoreResult | None = None,
    ) -> None:
        self.by_index = by_index
        self.single = single
        self.calls: list[str] = []

    async def score(self, call: CallForScoring) -> ScoreResult:
        """Dispatch on the ``#chunk<i>`` suffix the MeetingScorer assigns."""
        self.calls.append(call.call_ref)
        if "#chunk" in call.call_ref:
            idx = int(call.call_ref.split("#chunk")[1])
            return self.by_index[idx]
        assert self.single is not None
        return self.single


async def test_short_meeting_scored_in_single_pass() -> None:
    """A meeting within the cap is delegated verbatim to the base scorer once."""
    single = _result({1: 1, 2: 2})
    fake = _FakeScorer([], single=single)
    scorer = MeetingScorer(fake, rubric=RUBRIC, chunk_chars=10000)

    out = await scorer.score(
        CallForScoring(text="[agent] привет", duration_sec=600, call_ref="m2"),
    )

    assert out is single
    assert fake.calls == ["m2"]


async def test_long_meeting_merges_max_per_criterion_and_kev_bonus() -> None:
    """Long meeting: per-criterion max across chunks, union flags, +КЭВ bonus."""
    lines = [f"[agent] реплика {i} с достаточным объёмом текста" for i in range(60)]
    text = "\n".join(lines)
    chunks = chunk_transcript(text, max_chars=200, overlap_lines=1)
    n = len(chunks)
    assert n >= 2

    by_index = [_result({}) for _ in range(n)]
    by_index[0] = _result({1: 1}, summary="начало", tone="вежливый")
    by_index[-1] = _result(
        {4: 5},
        client_agreed=True,
        red_flags=["груб"],
        summary="конец",
        tone="грубый",
    )
    fake = _FakeScorer(by_index)
    scorer = MeetingScorer(
        fake,
        rubric=RUBRIC,
        chunk_chars=200,
        pass_threshold=75,
        kev_bonus_points=10,
    )

    out = await scorer.score(
        CallForScoring(text=text, duration_sec=3600, call_ref="m1"),
    )

    by_crit = {cs.id: cs.score for cs in out.criteria}
    assert by_crit[1] == 1
    assert by_crit[4] == 5
    assert out.total_score == 6
    assert out.client_agreed_meeting is True
    assert "груб" in out.red_flags
    assert out.manager_tone == "грубый"  # most negative wins
    assert out.meta["n_chunks"] == n
    assert out.meta["kev_bonus"] == 10
    # base_pct = 6/50*100 = 12.0; +10 КЭВ = 22.0
    assert out.meta["base_score_pct"] == 12.0
    assert out.score_pct == 22.0
    assert "начало" in out.summary
    assert "конец" in out.summary
