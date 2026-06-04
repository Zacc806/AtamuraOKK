"""Meeting scorer: map-reduce a long ОП-meeting into one rubric score (Этап 3).

Short meetings are scored in a single pass (delegated to the wrapped per-chunk
:class:`Scorer`). Long ones are chunked (:mod:`AtamuraOKK.scoring.meetings.chunking`),
each chunk scored independently, and the chunk results merged:

* per criterion -> **max** across chunks for stage-bound criteria (greeting,
  closing — they appear in one phase, best evidence wins), but **min** for
  ``rubric.min_merge_blocks`` (objections + soft skills): a clean chunk must not
  mask bad behaviour seen elsewhere, and the "full marks if no objection arose"
  rule applied per chunk would otherwise leak full objection marks;
* ``client_agreed_meeting`` -> any chunk; ``red_flags`` / ``script_deviations``
  -> de-duplicated union; ``script_adherence`` -> mean of present values;
  ``manager_tone`` -> the most negative observed (conservative for the KPI).

The merge is deterministic and pure given the chunk results, so it is fully
unit-testable with a fake per-chunk scorer.
"""

from __future__ import annotations

import asyncio

from AtamuraOKK.scoring.meetings.base import (
    CallForScoring,
    CriterionScore,
    Scorer,
    ScoreResult,
)
from AtamuraOKK.scoring.meetings.chunking import chunk_transcript
from AtamuraOKK.scoring.meetings.rubric import Rubric
from AtamuraOKK.transcription.cleanup import clean_transcript

# Most-negative-wins ordering for merging per-chunk tone labels.
_TONE_SEVERITY = {"вежливый": 0, "нейтральный": 1, "неуверенный": 2, "грубый": 3}
# Most-intense-wins ordering for merging per-chunk client-emotion labels (ТЗ 2.2).
_EMOTION_SEVERITY = {"спокоен": 0, "спешит": 1, "эмоционален": 2, "раздражён": 3}


def _dedup(items: list[str]) -> list[str]:
    """De-duplicate preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _worst_tone(tones: list[str]) -> str:
    """Pick the most negative known tone label; off-schema labels are ignored."""
    known = [t for t in tones if t in _TONE_SEVERITY]
    if not known:
        return "нейтральный"
    return max(known, key=lambda t: _TONE_SEVERITY[t])


def _peak_emotion(emotions: list[str]) -> str:
    """Pick the most intense known client emotion across chunks (surface issues)."""
    known = [e for e in emotions if e in _EMOTION_SEVERITY]
    if not known:
        return "спокоен"
    return max(known, key=lambda e: _EMOTION_SEVERITY[e])


class MeetingScorer:
    """Score a (possibly long) ОП-meeting, chunking when it exceeds the cap."""

    def __init__(
        self,
        base: Scorer,
        *,
        rubric: Rubric,
        chunk_chars: int,
        pass_threshold: int = 75,
        overlap_lines: int = 1,
        kev_bonus_points: int = 10,
    ) -> None:
        self.base = base
        self.rubric = rubric
        self.chunk_chars = chunk_chars
        self.pass_threshold = pass_threshold
        self.overlap_lines = overlap_lines
        self.kev_bonus_points = kev_bonus_points
        self._min_merge_blocks = frozenset(rubric.min_merge_blocks)

    async def score(self, call: CallForScoring) -> ScoreResult:
        """Score the meeting; single pass when it fits, else chunk + merge."""
        # Clean once and gate + chunk on the SAME text so the size decision and
        # the splitter never disagree (cleanup only shrinks, never grows).
        cleaned = clean_transcript(call.text)
        if len(cleaned) <= self.chunk_chars:
            return await self.base.score(call)

        chunks = chunk_transcript(
            cleaned,
            max_chars=self.chunk_chars,
            overlap_lines=self.overlap_lines,
        )
        sub_calls = [
            CallForScoring(
                text=chunk,
                duration_sec=call.duration_sec,
                language=call.language,
                language_probability=call.language_probability,
                call_ref=f"{call.call_ref}#chunk{i}",
                visit_index=call.visit_index,
            )
            for i, chunk in enumerate(chunks)
        ]
        results = await asyncio.gather(*(self.base.score(c) for c in sub_calls))
        return self._merge(list(results), call)

    def _merge(self, results: list[ScoreResult], call: CallForScoring) -> ScoreResult:
        seen: dict[int, list[CriterionScore]] = {}
        for res in results:
            for cs in res.criteria:
                seen.setdefault(cs.id, []).append(cs)
        chosen: dict[int, CriterionScore] = {}
        for cid, scores in seen.items():
            # MIN for whole-meeting / conditional blocks (worst chunk wins),
            # MAX otherwise (a stage-bound criterion appears in one chunk only).
            pick = min if scores[0].block in self._min_merge_blocks else max
            chosen[cid] = pick(scores, key=lambda c: c.score)
        criteria = [chosen[c.id] for c in self.rubric.criteria if c.id in chosen]

        total = sum(cs.score for cs in criteria)
        base_pct = round(total / self.rubric.max_total_score * 100, 1)
        client_agreed = any(r.client_agreed_meeting for r in results)
        kev_bonus = (
            self.kev_bonus_points
            if (self.kev_bonus_points and client_agreed)
            else 0
        )
        score_pct = min(100.0, round(base_pct + kev_bonus, 1))

        adherence = [
            r.script_adherence for r in results if r.script_adherence is not None
        ]
        script_adherence = (
            round(sum(adherence) / len(adherence), 1) if adherence else None
        )

        meta: dict[str, object] = {"n_chunks": len(results), "base_score_pct": base_pct}
        meta["client_emotion"] = _peak_emotion(
            [str(r.meta.get("client_emotion", "")) for r in results],
        )
        meta["visit_index"] = call.visit_index
        if kev_bonus:
            meta["kev_bonus"] = kev_bonus

        return ScoreResult(
            rubric_version=self.rubric.id,
            total_score=total,
            max_total=self.rubric.max_total_score,
            score_pct=score_pct,
            passed=score_pct >= self.pass_threshold,
            criteria=criteria,
            call_type=results[0].call_type,
            client_agreed_meeting=client_agreed,
            manager_tone=_worst_tone([r.manager_tone for r in results]),
            red_flags=_dedup([f for r in results for f in r.red_flags]),
            summary=" ".join(r.summary for r in results if r.summary).strip(),
            language=call.language,
            provider=results[0].provider,
            model=results[0].model,
            needs_human_review=any(r.needs_human_review for r in results),
            script_adherence=script_adherence,
            script_deviations=_dedup([d for r in results for d in r.script_deviations]),
            meta=meta,
        )
