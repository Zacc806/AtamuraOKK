"""Cash-buyer manager alert: trigger predicate + Bitrix notification flow.

A scored qualification call where the client pays cash notifies the responsible
manager in Bitrix exactly once. A re-score must not re-notify (idempotent via
``scores.notified_at``), and a Bitrix failure must never fail the score.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.bitrix import BitrixError
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallDirection, CallStatus
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.db.models.score import Score
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.scoring.base import CallScore, CriterionScore
from AtamuraOKK.scoring.rubric import Rubric
from AtamuraOKK.scoring.rubric import load_rubric as _load_rubric
from AtamuraOKK.scoring.worker import _score_one, should_notify_cash


def _cash_call_score(
    rubric: Rubric,
    *,
    payment_method: str = "наличные",
    is_qualification_call: bool = True,
    wants_to_visit: bool = True,
    on_premises: bool = False,
) -> CallScore:
    """A full-marks CallScore with configurable sales signals."""
    return CallScore(
        call_type="квалификация",
        is_qualification_call=is_qualification_call,
        manager_identified=True,
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
        objections_present=True,
        sentiment_customer="нейтральный",
        sentiment_agent="нейтральный",
        summary="Клиент берёт за наличные, готов приехать.",
        red_flags=[],
        target_status="целевой",
        strengths="-",
        growth_zone="-",
        training_recommendation="-",
        payment_method=payment_method,  # type: ignore[arg-type]
        wants_to_visit=wants_to_visit,
        on_premises=on_premises,
    )


class _FakeScorer:
    """Returns a fixed CallScore; records how many times it ran."""

    model_label = "fake/test"

    def __init__(self, result: CallScore) -> None:
        self._result = result
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
        return self._result


class _FakeNotifier:
    """Records every notification it is asked to send."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send(self, user_id: int, message: str) -> bool:
        self.sent.append((user_id, message))
        return True


class _RaisingNotifier:
    """A notifier that always fails (e.g. webhook lacks the `im` scope)."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, user_id: int, message: str) -> bool:
        self.attempts += 1
        raise BitrixError("INSUFFICIENT_SCOPE", "no im scope", "im.notify.personal.add")


# --- pure predicate ---------------------------------------------------------

_NOW = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)


def test_predicate_cash_qualification_recent_fires() -> None:
    """Cash + qualification + recent → fire."""
    rubric = _load_rubric()
    score = _cash_call_score(rubric)
    assert should_notify_cash(
        score, started_at=_NOW - timedelta(minutes=5), now=_NOW, max_age_minutes=60
    )


def test_predicate_non_cash_does_not_fire() -> None:
    """A non-cash payment method never fires."""
    rubric = _load_rubric()
    score = _cash_call_score(rubric, payment_method="ипотека")
    assert not should_notify_cash(score, started_at=_NOW, now=_NOW, max_age_minutes=60)


def test_predicate_non_qualification_does_not_fire() -> None:
    """A non-qualification call (reminder/vendor/etc.) never fires."""
    rubric = _load_rubric()
    score = _cash_call_score(rubric, is_qualification_call=False)
    assert not should_notify_cash(score, started_at=_NOW, now=_NOW, max_age_minutes=60)


def test_predicate_stale_call_does_not_fire() -> None:
    """The age guard blocks a backfill / rescore of old calls."""
    rubric = _load_rubric()
    score = _cash_call_score(rubric)
    assert not should_notify_cash(
        score, started_at=_NOW - timedelta(minutes=90), now=_NOW, max_age_minutes=60
    )


def test_predicate_no_started_at_does_not_fire() -> None:
    """A call without a start time can't pass the recency guard."""
    rubric = _load_rubric()
    score = _cash_call_score(rubric)
    assert not should_notify_cash(score, started_at=None, now=_NOW, max_age_minutes=60)


# --- end-to-end through _score_one -----------------------------------------


async def _seed_call(session: AsyncSession, *, bitrix_call_id: str) -> Call:
    """A recent, claimed (SCORING) call attributed to a manager with a Bitrix id."""
    manager = Manager(bitrix_user_id=777)
    session.add(manager)
    await session.flush()
    call = Call(
        bitrix_call_id=bitrix_call_id,
        status=CallStatus.SCORING,
        direction=CallDirection.OUTBOUND,
        analyzable=True,
        manager_id=manager.id,
        started_at=datetime.now(UTC),
        crm_entity_type="DEAL",
        crm_entity_id=4242,
    )
    session.add(call)
    await session.flush()
    session.add(Transcript(call_id=call.id, full_text="[AGENT] привет"))
    await session.flush()
    return call


async def test_cash_buyer_notifies_manager_once(dbsession: AsyncSession) -> None:
    """A cash-buyer call notifies the manager; a re-score does not re-notify."""
    rubric = _load_rubric()
    scorer = _FakeScorer(_cash_call_score(rubric))
    notifier = _FakeNotifier()
    call = await _seed_call(dbsession, bitrix_call_id="cash-1")
    transcript = await dbsession.scalar(
        select(Transcript).where(Transcript.call_id == call.id),
    )
    assert transcript is not None

    await _score_one(dbsession, call, transcript, scorer, rubric, notifier)

    assert len(notifier.sent) == 1
    user_id, message = notifier.sent[0]
    assert user_id == 777
    assert "наличными" in message
    assert "crm/deal/details/4242" in message  # CRM card link included

    score = await dbsession.scalar(select(Score).where(Score.call_id == call.id))
    assert score is not None and score.notified_at is not None

    # Re-score: same row upserts, notified_at survives, no second notification.
    call.status = CallStatus.SCORING
    await _score_one(dbsession, call, transcript, scorer, rubric, notifier)
    assert len(notifier.sent) == 1


async def test_non_cash_call_does_not_notify(dbsession: AsyncSession) -> None:
    """A non-cash buyer produces no notification."""
    rubric = _load_rubric()
    scorer = _FakeScorer(_cash_call_score(rubric, payment_method="ипотека"))
    notifier = _FakeNotifier()
    call = await _seed_call(dbsession, bitrix_call_id="cash-2")
    transcript = await dbsession.scalar(
        select(Transcript).where(Transcript.call_id == call.id),
    )
    assert transcript is not None

    await _score_one(dbsession, call, transcript, scorer, rubric, notifier)

    assert notifier.sent == []


async def test_bitrix_failure_does_not_fail_score(dbsession: AsyncSession) -> None:
    """A Bitrix error is swallowed: the call stays SCORED, notified_at stays NULL."""
    rubric = _load_rubric()
    scorer = _FakeScorer(_cash_call_score(rubric))
    notifier = _RaisingNotifier()
    call = await _seed_call(dbsession, bitrix_call_id="cash-3")
    transcript = await dbsession.scalar(
        select(Transcript).where(Transcript.call_id == call.id),
    )
    assert transcript is not None

    await _score_one(dbsession, call, transcript, scorer, rubric, notifier)

    assert notifier.attempts == 1
    assert call.status == CallStatus.SCORED
    score = await dbsession.scalar(select(Score).where(Score.call_id == call.id))
    assert score is not None and score.notified_at is None
