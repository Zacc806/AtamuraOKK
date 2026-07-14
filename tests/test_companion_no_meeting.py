"""«По оценке ОКК» callback queue — целевые calls that never got a meeting booked.

The queue's verdict comes from the rubric's closing block: «Зафиксировал дату +
время записи в ОП» = НЕТ means nothing reached the calendar, so the client is
still callable. Two things make that fragile, and these tests pin both:

* the criterion **ids are version-specific** (only ``block_id`` is stable), so a
  renumbered rubric would silently empty or corrupt the queue — ``_CLOSING_CRITERIA``
  is checked against the real tm-call-v4 rubric file;
* Н.П. elements are **absent** from ``per_criterion`` (the scorer drops them so they
  leave the denominator), so a missing id must never be read as a НЕТ.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from AtamuraOKK.scoring.rubric import load_rubric
from AtamuraOKK.web.api.v1.day import (
    _CLOSING_CRITERIA,
    _closing_scores,
    _fill_client_names,
    _no_meeting_reason,
)
from AtamuraOKK.web.api.v1.schemas import NoMeetingItem

pytestmark = pytest.mark.anyio

_V4 = "tm-call-v4"
_IDS = _CLOSING_CRITERIA[_V4]


def _pc(cid: int, score: int, block_id: str = "closing") -> dict[str, Any]:
    return {"id": cid, "block_id": block_id, "score": score}


class FakeBitrix:
    """Replays the one read _fill_client_names makes: crm.contact.list."""

    def __init__(self, contacts: list[dict[str, Any]]) -> None:
        self.contacts = contacts
        self.calls: list[str] = []

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield the requested contacts, recording that the call was made."""
        self.calls.append(method)
        wanted = set((params or {}).get("filter", {}).get("ID") or [])
        for c in self.contacts:
            if int(c["ID"]) in wanted:
                yield c


def test_closing_criteria_ids_match_the_live_rubric() -> None:
    """The ids we key on are really the closing elements of the active rubric.

    A rubric renumber must break this test, not the queue: the wrong id would
    make «не дожали» read some unrelated element and quietly mis-file clients.
    """
    rubric = load_rubric(_V4)
    closing = next(b for b in rubric.block_list if b.id == "closing")
    by_id = {c.id: c.text for c in closing.criteria}

    assert set(_IDS.values()) <= set(by_id), "closing ids drifted from the rubric"
    assert by_id[_IDS["booked"]].startswith("Зафиксировал дату")
    assert by_id[_IDS["time"]].startswith("Предложил конкретное время")
    assert by_id[_IDS["retry"]].startswith("При отказе")
    assert by_id[_IDS["value"]].startswith("Презентовал ценность")


def test_closing_scores_keeps_only_the_closing_block() -> None:
    """Other blocks share the same id space visually — never mix them in."""
    criteria = {
        "per_criterion": [
            _pc(_IDS["booked"], 0),
            _pc(9, 1, block_id="programming"),
            _pc(31, 0, block_id="objections"),
        ],
    }
    assert _closing_scores(criteria) == {_IDS["booked"]: 0}


def test_closing_scores_omits_not_applicable_elements() -> None:
    """Н.П. elements never appear in per_criterion — absence is not a НЕТ.

    «При отказе — повторная попытка закрыть» is Н.П. when the client agreed at
    once; reading that absence as a failure would blame the manager for the one
    thing they did right.
    """
    criteria = {"per_criterion": [_pc(_IDS["booked"], 0), _pc(_IDS["value"], 1)]}
    scores = _closing_scores(criteria)

    assert _IDS["retry"] not in scores
    assert scores.get(_IDS["retry"]) != 0  # the guard the queue relies on
    assert _no_meeting_reason(scores, _IDS) == "дату и время записи не зафиксировали"


def test_no_meeting_reason_prefers_the_unanswered_doubt() -> None:
    """An abandoned objection outranks a missing time slot — it's the real miss."""
    scores = {_IDS["booked"]: 0, _IDS["retry"]: 0, _IDS["time"]: 0, _IDS["value"]: 0}
    gave_up = "клиент засомневался — дожать не пытались"
    assert _no_meeting_reason(scores, _IDS) == gave_up


def test_no_meeting_reason_falls_through_to_the_next_miss() -> None:
    """With the doubt handled, the next actionable gap surfaces instead."""
    scores = {_IDS["booked"]: 0, _IDS["retry"]: 1, _IDS["time"]: 0, _IDS["value"]: 0}
    no_time = "конкретное время встречи не предложили"
    assert _no_meeting_reason(scores, _IDS) == no_time

    scores = {_IDS["booked"]: 0, _IDS["retry"]: 1, _IDS["time"]: 1, _IDS["value"]: 0}
    assert _no_meeting_reason(scores, _IDS) == "позвал на встречу без ценности"


def test_no_meeting_reason_when_only_the_booking_slipped() -> None:
    """Invited well, offered a time, client didn't refuse — just never written down."""
    scores = {_IDS["booked"]: 0, _IDS["retry"]: 1, _IDS["time"]: 1, _IDS["value"]: 1}
    assert _no_meeting_reason(scores, _IDS) == "дату и время записи не зафиксировали"


def _item(call_id: int, contact_id: int | None, phone: str) -> NoMeetingItem:
    return NoMeetingItem(call_id=call_id, contact_id=contact_id, phone=phone)


async def test_fill_client_names_names_the_contact_linked_cards() -> None:
    """A name makes the card dialable-by-a-human; the phone alone is enough to work."""
    linked = _item(1, 4001, "+77770000001")
    bare = _item(2, None, "+77770000002")  # deal/lead-linked: no resolvable name
    bx = FakeBitrix([{"ID": "4001", "NAME": "Исламхан", "LAST_NAME": ""}])

    await _fill_client_names(bx, [linked, bare])  # type: ignore[arg-type]

    assert linked.client_name == "Исламхан"
    assert bare.client_name is None
    assert bare.phone == "+77770000002"  # still callable — the point of the queue


async def test_fill_client_names_skips_bitrix_when_nothing_to_resolve() -> None:
    """No contact-linked cards → no Bitrix round-trip at all."""
    bx = FakeBitrix([])
    await _fill_client_names(bx, [_item(1, None, "+77770000003")])  # type: ignore[arg-type]
    assert bx.calls == []


def test_legacy_rubric_yields_no_verdict() -> None:
    """tm-call-v2 lumped the close into one weighted element — it cannot answer this.

    Guessing from it would put clients in the queue on a criterion that never
    asserted a booking, so the queue must stay silent for those calls.
    """
    assert "tm-call-v2" not in _CLOSING_CRITERIA
