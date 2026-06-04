"""Tests for the manipulation detector (ТЗ 2.1, fake Claude client)."""

from __future__ import annotations

from typing import Any

from AtamuraOKK.scoring.manipulation import (
    ManipulationDetector,
    _parse_manipulations,
)
from AtamuraOKK.scoring.zhk import ZhkFacts


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class _Messages:
    def __init__(self, text: str) -> None:
        self._text = text

    async def create(self, **_kwargs: Any) -> _Resp:
        return _Resp(self._text)


class _FakeClient:
    """Stand-in for AsyncAnthropic exposing only messages.create."""

    def __init__(self, text: str) -> None:
        self.messages = _Messages(text)


def test_parse_tolerates_fences_and_prose() -> None:
    """A fenced JSON array amid prose is parsed into Manipulation objects."""
    raw = (
        "Нашёл:\n```json\n"
        '[{"zhk":"Аура","claim":"есть лифт","reality":"лифта нет",'
        '"severity":"high"}]\n```'
    )
    out = _parse_manipulations(raw)
    assert len(out) == 1
    assert out[0].zhk == "Аура"
    assert out[0].severity == "high"


def test_parse_garbage_returns_empty() -> None:
    """Non-JSON answers degrade to an empty list, never raise."""
    assert _parse_manipulations("нарушений нет") == []


async def test_empty_kb_skips_llm() -> None:
    """With no ЖК facts loaded the detector returns [] without calling the LLM."""
    detector = ManipulationDetector([], client=_FakeClient("unused"))
    assert await detector.detect("[agent] у нас есть лифт") == []


async def test_detect_flags_contradiction() -> None:
    """Given facts + a contradicting claim, the detector returns the flag."""
    facts = [ZhkFacts(name="Аура", has_elevator=False, floors=6)]
    answer = (
        '[{"zhk":"Аура","claim":"сказал что есть лифт",'
        '"reality":"в Ауре лифта нет","severity":"high"}]'
    )
    detector = ManipulationDetector(facts, client=_FakeClient(answer))

    out = await detector.detect("[agent] у нас есть лифт на этаже")

    assert len(out) == 1
    assert out[0].zhk == "Аура"
    assert "лифт" in out[0].claim
