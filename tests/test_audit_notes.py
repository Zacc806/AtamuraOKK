"""Deal-card notes as judge evidence: what counts as a note, and what never sees one.

The point of the module is that a close reason is often documented on the card and not
said on the call, so the judge must see the manager's notes. The risk is the opposite of
missing them: 55% of this portal's timeline comments are Wazzup/WhatsApp integration
traffic, which would drown the real notes in the prompt — so most of these tests are
about what gets thrown away, and about the rule routes never consulting notes at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from AtamuraOKK.audit import notes
from AtamuraOKK.settings import settings

pytestmark = pytest.mark.anyio

_WAZZUP = (
    "[IMG]https://static.wazzup24.com/images/bitrix/whatsapp.png[/IMG] "
    "Мерей Толегенова: Здравствуйте! Хотела пригласить на консультацию."
)


class _FakeBitrix:
    """Serves crm.timeline.comment.list batches; records what it was asked for."""

    def __init__(
        self,
        comments: dict[int, list[dict[str, Any]]],
        *,
        raises: bool = False,
    ) -> None:
        self.comments = comments
        self.raises = raises
        self.batches: list[dict[str, Any]] = []

    async def batch(self, commands: dict[str, Any]) -> dict[str, Any]:
        if self.raises:
            raise RuntimeError("Bitrix down")
        self.batches.append(commands)
        for method, _params in commands.values():
            assert method == "crm.timeline.comment.list"
        return {
            key: self.comments.get(int(key), []) for key in commands if int(key) != 0
        }


def _c(text: str) -> dict[str, Any]:
    return {"ID": "1", "COMMENT": text, "AUTHOR_ID": "555", "CREATED": "2026-07-01"}


def test_clean_note_strips_markup_and_keeps_the_words() -> None:
    """BB-code, media blocks, bare URLs and &nbsp; are markup, not the note."""
    raw = "[B]ндз[/B]&nbsp;перезвонить [URL=x]тут[/URL] https://example.com/a завтра"
    assert notes.clean_note(raw) == "ндз перезвонить завтра"


def test_clean_note_drops_what_is_too_short_to_be_a_note() -> None:
    """A comment that is only markup (or a stray char) leaves nothing to judge on."""
    assert notes.clean_note("[IMG]https://x/y.png[/IMG]") == ""
    assert notes.clean_note("-") == ""


async def test_wazzup_traffic_is_not_a_note() -> None:
    """Messenger-integration output is machine traffic — it must never reach the judge.

    This is the whole reason the filter exists: unfiltered, these outnumber the real
    notes and the judge would weigh the team's own outbound templates as evidence.
    """
    bx = _FakeBitrix({77: [_c(_WAZZUP), _c("клиент просит перезвонить в 18")]})

    got = await notes.notes_for_deals(bx, [77])

    assert got == {77: ["клиент просит перезвонить в 18"]}


async def test_notes_are_returned_per_deal_oldest_first() -> None:
    """Each deal gets its own notes, in the order they were written."""
    bx = _FakeBitrix(
        {
            11: [_c("пв есть 15 млн"), _c("приедет в оп 12го")],
            12: [_c("не актуально")],
            13: [],
        }
    )

    got = await notes.notes_for_deals(bx, [11, 12, 13])

    assert got == {
        11: ["пв есть 15 млн", "приедет в оп 12го"],
        12: ["не актуально"],
        13: [],
    }


async def test_note_count_cap_keeps_the_newest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notes cluster around the close, so a long card drops its *oldest* notes."""
    monkeypatch.setattr(settings, "audit_note_max_count", 2)
    bx = _FakeBitrix({9: [_c("первая"), _c("вторая"), _c("третья")]})

    got = await notes.notes_for_deals(bx, [9])

    assert got == {9: ["вторая", "третья"]}


def test_format_notes_respects_the_char_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prompt block is bounded: a runaway card cannot blow up the judge request."""
    monkeypatch.setattr(settings, "audit_note_max_chars", 10)

    assert notes.format_notes(["короткая", "вторая которая уже не влезает"]) == (
        "- короткая"
    )
    assert notes.format_notes([]) == ""


async def test_bitrix_failure_degrades_to_no_notes() -> None:
    """Notes are supplementary: losing them must not fail the pass or the judge."""
    bx = _FakeBitrix({}, raises=True)

    got = await notes.notes_for_deals(bx, [1, 2])

    assert got == {1: [], 2: []}


async def test_switch_off_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the knob off no Bitrix read happens at all (not just an ignored result)."""
    monkeypatch.setattr(settings, "audit_use_card_notes", False)
    bx = _FakeBitrix({5: [_c("важная заметка")]})

    got = await notes.notes_for_deals(bx, [5])

    assert got == {5: []}
    assert not bx.batches


async def test_batches_are_chunked_to_the_bitrix_page_size() -> None:
    """No cross-entity filter exists, so 120 deals must still be 3 reads, not 120."""
    from AtamuraOKK.bitrix.client import PAGE_SIZE

    ids = list(range(1, 121))
    bx = _FakeBitrix({i: [_c(f"заметка {i}")] for i in ids})

    got = await notes.notes_for_deals(bx, ids)

    assert len(got) == 120
    assert len(bx.batches) == 3
    assert all(len(b) <= PAGE_SIZE for b in bx.batches)
