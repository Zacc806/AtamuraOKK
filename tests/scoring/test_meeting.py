"""Tests for the map-reduce MeetingScorer (fake per-chunk scorer, no network)."""

from __future__ import annotations

from AtamuraOKK.scoring.meetings.base import CallForScoring, CriterionScore, ScoreResult
from AtamuraOKK.scoring.meetings.chunking import chunk_transcript
from AtamuraOKK.scoring.meetings.meeting import MeetingScorer, _worst_tone
from AtamuraOKK.scoring.meetings.rubric import load_rubric

RUBRIC = load_rubric("okk_meeting_v1")


def _long_text(n: int = 60) -> str:
    """A transcript long enough to force chunking at chunk_chars=200."""
    return "\n".join(
        f"[agent] реплика {i} с достаточным объёмом текста" for i in range(n)
    )


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


async def test_objection_block_merges_by_min_not_max() -> None:
    """A no-objection chunk must not mask bad objection handling elsewhere.

    Objection criteria (12-15) are in min_merge_blocks, so the worst chunk wins
    — otherwise the per-chunk 'full marks if no objection' rule would leak full
    marks via MAX and erase the badly-handled objection.
    """
    text = _long_text()
    n = len(chunk_transcript(text, max_chars=200, overlap_lines=1))
    assert n >= 2
    # Every chunk reports full objection marks (no objection seen) EXCEPT the
    # last, where the objection was handled badly (zeros).
    by_index = [_result({12: 1, 13: 2, 14: 2, 15: 3}) for _ in range(n)]
    by_index[-1] = _result({12: 0, 13: 0, 14: 0, 15: 0})
    scorer = MeetingScorer(_FakeScorer(by_index), rubric=RUBRIC, chunk_chars=200)

    out = await scorer.score(
        CallForScoring(text=text, duration_sec=3600, call_ref="m"),
    )

    by_crit = {cs.id: cs.score for cs in out.criteria}
    assert by_crit[12] == 0
    assert by_crit[13] == 0
    assert by_crit[15] == 0  # min wins -> bad handling counts, not masked


async def test_soft_skill_block_merges_by_min() -> None:
    """Whole-meeting soft skills take the worst chunk, not the best."""
    text = _long_text()
    n = len(chunk_transcript(text, max_chars=200, overlap_lines=1))
    by_index = [_result({19: 2, 20: 2}) for _ in range(n)]
    by_index[-1] = _result({19: 0, 20: 0})  # rude/sloppy in one chunk
    scorer = MeetingScorer(_FakeScorer(by_index), rubric=RUBRIC, chunk_chars=200)

    out = await scorer.score(
        CallForScoring(text=text, duration_sec=3600, call_ref="m"),
    )

    by_crit = {cs.id: cs.score for cs in out.criteria}
    assert by_crit[19] == 0
    assert by_crit[20] == 0


def test_worst_tone_ignores_off_schema_labels() -> None:
    """Unknown tone labels are ignored, not ranked as neutral."""
    assert _worst_tone(["вежливый", "polite"]) == "вежливый"
    assert _worst_tone(["вежливый", "грубый"]) == "грубый"
    assert _worst_tone(["мусор"]) == "нейтральный"


class _RecordingScorer:
    """Fake per-chunk scorer that records every transcript text it was given."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def score(self, call: CallForScoring) -> ScoreResult:
        self.texts.append(call.text)
        return _result({1: 1})


async def test_long_single_line_meeting_is_chunked_not_truncated() -> None:
    """A whisper-style transcript with no newlines fans out into chunks.

    Regression for the truncation bug: the whole meeting — including the very
    last sentence — must reach the per-chunk scorer.
    """
    sentences = [f"Менеджер рассказывает про этап {i} сделки." for i in range(200)]
    text = " ".join(sentences)
    fake = _RecordingScorer()
    scorer = MeetingScorer(fake, rubric=RUBRIC, chunk_chars=600)

    out = await scorer.score(CallForScoring(text=text, duration_sec=5400, call_ref="m"))

    assert len(fake.texts) > 1  # actually chunked, not a single oversized pass
    assert all(len(t) <= 600 for t in fake.texts)  # no chunk can be truncated
    seen = " ".join(" ".join(t.splitlines()) for t in fake.texts)
    assert sentences[-1] in seen  # the tail of the meeting was scored
    assert out.meta["n_chunks"] == len(fake.texts)


async def test_chunk_concurrency_is_bounded() -> None:
    """No more than chunk_concurrency chunks are in flight at once."""
    import asyncio

    class _GaugeScorer:
        def __init__(self) -> None:
            self.active = 0
            self.peak = 0

        async def score(self, call: CallForScoring) -> ScoreResult:
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0)
            self.active -= 1
            return _result({1: 1})

    fake = _GaugeScorer()
    scorer = MeetingScorer(fake, rubric=RUBRIC, chunk_chars=200, chunk_concurrency=2)
    await scorer.score(CallForScoring(text=_long_text(), duration_sec=5400))

    assert fake.peak <= 2
