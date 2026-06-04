"""build_prompt must adapt call vs ОП-meeting framing from rubric.context."""

from __future__ import annotations

from AtamuraOKK.scoring.prompts import build_prompt
from AtamuraOKK.scoring.rubric import load_rubric

_TEXT = "[agent] здравствуйте\n[customer] добрый день"


def _prompt(version: str) -> str:
    return build_prompt(
        load_rubric(version),
        text=_TEXT,
        duration_sec=2400,
        max_chars=10000,
    )


def test_meeting_rubric_uses_meeting_framing() -> None:
    """okk_meeting_v1 (context op_meeting) yields meeting wording + fragment note."""
    prompt = _prompt("okk_meeting_v1")
    assert "встречу менеджера ОП" in prompt
    assert "ТИП ВСТРЕЧИ" in prompt
    assert "Длительность встречи" in prompt
    assert "ФРАГМЕНТ длинной встречи" in prompt
    assert "ТИП ЗВОНКА" not in prompt
    assert "Длительность звонка" not in prompt


def test_call_rubric_uses_call_framing() -> None:
    """A call rubric keeps the call wording and the call-type block."""
    prompt = _prompt("tm_call_v3")
    assert "звонок менеджера" in prompt
    assert "ТИП ЗВОНКА" in prompt
    assert "Длительность звонка" in prompt
    assert "ТИП ВСТРЕЧИ" not in prompt
    assert "ФРАГМЕНТ длинной встречи" not in prompt


def test_both_include_kazakh_greeting_rule() -> None:
    """KZ-greeting leniency applies regardless of call/meeting framing."""
    for version in ("okk_meeting_v1", "tm_call_v3"):
        assert "Сәлеметсіз бе" in _prompt(version)
