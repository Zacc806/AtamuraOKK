"""Tests for category-aware scoring-prompt construction (no DB / no LLM)."""

from __future__ import annotations

from AtamuraOKK.scoring.prompt import build_messages
from AtamuraOKK.scoring.rubric import load_rubric


def _user(messages: list[dict[str, str]]) -> str:
    return next(m["content"] for m in messages if m["role"] == "user")


def _checklist(user: str) -> str:
    """The checklist section, between the ЧЕК-ЛИСТ header and the transcript."""
    return user.split("ЧЕК-ЛИСТ", 1)[1].split("ТРАНСКРИПТ", 1)[0]


def test_prompt_b_shows_reduced_closing_max() -> None:
    """Category B presents «Закрытие на КЭВ» on the reduced (18) scale."""
    rubric = load_rubric()
    user = _user(build_messages("[AGENT] привет", rubric, "outbound", "B"))

    assert "Категория клиента: B" in user
    checklist = _checklist(user)
    assert "## Закрытие на КЭВ" in checklist
    assert "max 18" in checklist
    assert "max 37" not in checklist


def test_prompt_c_excludes_closing_from_checklist() -> None:
    """Category C drops the closing criterion from the checklist entirely."""
    rubric = load_rubric()
    user = _user(build_messages("[AGENT] привет", rubric, "outbound", "C"))

    assert "Категория клиента: C" in user
    assert "НЕ оценивается" in user  # the C closing instruction is present
    assert "## Закрытие на КЭВ" not in _checklist(user)


def test_prompt_default_full_closing() -> None:
    """No category -> full closing weight, no special instruction."""
    rubric = load_rubric()
    user = _user(build_messages("[AGENT] привет", rubric, "outbound", None))

    assert "Категория клиента: не указана" in user
    assert "max 37" in _checklist(user)
