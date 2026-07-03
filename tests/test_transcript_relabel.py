"""Reconcile inverted client/manager transcript labels with the scored manager.

Some stereo calls record the manager on the channel we label as the customer (and
vice versa). The scorer reports ``manager_side``; the scoring worker swaps the
speaker labels when the manager is on side "B", once, preserving the spoken text
(including entity-corrected ЖК names that live only in ``full_text``).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallDirection, CallStatus
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.scoring.base import CallScore, CriterionScore
from AtamuraOKK.scoring.rubric import Rubric
from AtamuraOKK.scoring.rubric import load_rubric as _load_rubric
from AtamuraOKK.scoring.worker import (
    _reconcile_transcript_labels,
    _score_one,
    _swap_speaker_labels,
)


def _segments() -> list[dict[str, object]]:
    return [
        {"speaker": "agent", "start": 0.0, "end": 1.0, "text": "алло, ЖК Астана"},
        {"speaker": "customer", "start": 1.0, "end": 2.0, "text": "да, слушаю"},
    ]


def _score(manager_side: str) -> CallScore:
    return CallScore(
        call_type="квалификация",
        is_qualification_call=True,
        manager_identified=manager_side != "unknown",
        manager_side=manager_side,
        criteria=[],
        objections_present=False,
        sentiment_customer="нейтральный",
        sentiment_agent="нейтральный",
        summary="тест",
        red_flags=[],
        target_status="неясно",
        strengths="-",
        growth_zone="-",
        training_recommendation="-",
    )


def test_swap_speaker_labels_swaps_roles_and_preserves_body() -> None:
    """Segment speakers swap; [AGENT]/[CUSTOMER] headers swap; text is untouched."""
    # full_text body intentionally differs from the segment text (entity correction
    # edits full_text only) — the swap must not rebuild it from segments.
    full_text = "[AGENT]\nалло, ЖК «Астана Сити»\n\n[CUSTOMER]\nда, слушаю"
    segs, new_full = _swap_speaker_labels(_segments(), full_text)

    assert [s["speaker"] for s in segs] == ["customer", "agent"]
    assert [s["text"] for s in segs] == ["алло, ЖК Астана", "да, слушаю"]
    # Headers swapped, the corrected body ("Астана Сити") preserved verbatim.
    assert new_full == "[CUSTOMER]\nалло, ЖК «Астана Сити»\n\n[AGENT]\nда, слушаю"


def test_reconcile_flips_when_manager_on_side_b() -> None:
    """Manager on side B -> labels swapped and the transcript marked reconciled."""
    t = Transcript(
        call_id=1,
        full_text="[AGENT]\nволос\n\n[CUSTOMER]\nответ",
        segments=_segments(),
        manager_side_applied=False,
    )
    _reconcile_transcript_labels(t, _score("B"))
    assert [s["speaker"] for s in t.segments] == ["customer", "agent"]
    assert t.full_text == "[CUSTOMER]\nволос\n\n[AGENT]\nответ"
    assert t.manager_side_applied is True


def test_reconcile_noop_when_manager_on_side_a() -> None:
    """Manager on side A -> labels unchanged but still marked reconciled."""
    t = Transcript(
        call_id=1,
        full_text="[AGENT]\nволос\n\n[CUSTOMER]\nответ",
        segments=_segments(),
        manager_side_applied=False,
    )
    _reconcile_transcript_labels(t, _score("A"))
    assert [s["speaker"] for s in t.segments] == ["agent", "customer"]
    assert t.full_text == "[AGENT]\nволос\n\n[CUSTOMER]\nответ"
    # Marked reconciled so a later re-score skips it.
    assert t.manager_side_applied is True


def test_reconcile_noop_when_manager_unknown() -> None:
    """Unknown manager -> nothing changes and the transcript stays unreconciled."""
    t = Transcript(
        call_id=1,
        full_text="[UNKNOWN]\nневнятно",
        segments=[{"speaker": "unknown", "start": 0.0, "end": 1.0, "text": "x"}],
        manager_side_applied=False,
    )
    _reconcile_transcript_labels(t, _score("unknown"))
    assert [s["speaker"] for s in t.segments] == ["unknown"]
    # Left unreconciled — a later pass that does identify a side can still fix it.
    assert t.manager_side_applied is False


def test_reconcile_is_idempotent_once_applied() -> None:
    """An already-reconciled transcript is never flipped again, even on side B."""
    t = Transcript(
        call_id=1,
        full_text="[AGENT]\nволос\n\n[CUSTOMER]\nответ",
        segments=_segments(),
        manager_side_applied=True,
    )
    _reconcile_transcript_labels(t, _score("B"))
    assert [s["speaker"] for s in t.segments] == ["agent", "customer"]
    assert t.full_text == "[AGENT]\nволос\n\n[CUSTOMER]\nответ"


class _SideScorer:
    """Always reports the manager on side B (inverted channel mapping)."""

    model_label = "fake/test"

    def __init__(self) -> None:
        self.calls = 0

    async def score(
        self,
        *,
        transcript: str,
        rubric: Rubric,
        direction: str,
        client_category: str | None = None,
    ) -> CallScore:
        self.calls += 1
        return CallScore(
            call_type="квалификация",
            is_qualification_call=True,
            manager_identified=True,
            manager_side="B",
            criteria=[
                CriterionScore(
                    id=c.id,
                    score=c.max,
                    justification="ok",
                    evidence="",
                    recommendation="-",
                )
                for c in rubric.scored_criteria
            ],
            objections_present=False,
            sentiment_customer="нейтральный",
            sentiment_agent="нейтральный",
            summary="тест",
            red_flags=[],
            target_status="неясно",
            strengths="-",
            growth_zone="-",
            training_recommendation="-",
        )


async def test_score_one_flips_labels_once_across_rescore(
    dbsession: AsyncSession,
) -> None:
    """Scoring flips inverted labels; a re-score does not double-flip them back."""
    rubric = _load_rubric()
    scorer = _SideScorer()

    call = Call(
        bitrix_call_id="relabel-1",
        status=CallStatus.SCORING,
        direction=CallDirection.OUTBOUND,
        analyzable=True,
    )
    dbsession.add(call)
    await dbsession.flush()
    transcript = Transcript(
        call_id=call.id,
        full_text="[AGENT]\nалло\n\n[CUSTOMER]\nда",
        segments=_segments(),
        manager_side_applied=False,
    )
    dbsession.add(transcript)
    await dbsession.flush()

    await _score_one(dbsession, call, transcript, scorer, rubric)
    await _score_one(dbsession, call, transcript, scorer, rubric)

    row = await dbsession.scalar(
        select(Transcript).where(Transcript.call_id == call.id),
    )
    assert row is not None
    assert row.manager_side_applied is True
    # Flipped exactly once: the manager's line now carries the agent label.
    assert [s["speaker"] for s in row.segments] == ["customer", "agent"]
    assert row.full_text == "[CUSTOMER]\nалло\n\n[AGENT]\nда"
    assert scorer.calls == 2
