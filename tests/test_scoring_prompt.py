"""Tests for the binary scoring-prompt construction (no DB / no LLM)."""

from __future__ import annotations

from AtamuraOKK.scoring.prompt import build_messages
from AtamuraOKK.scoring.rubric import load_rubric


def _user(messages: list[dict[str, str]]) -> str:
    return next(m["content"] for m in messages if m["role"] == "user")


def _checklist(user: str) -> str:
    """The checklist section, between the ЧЕК-ЛИСТ header and the transcript."""
    return user.split("ЧЕК-ЛИСТ", 1)[1].split("ТРАНСКРИПТ", 1)[0]


def test_prompt_lists_every_block_and_element() -> None:
    """All 8 blocks and 34 elements are rendered with ДА/НЕТ rules."""
    rubric = load_rubric()
    checklist = _checklist(_user(build_messages("[AGENT] привет", rubric, "outbound")))

    for block in rubric.block_list:
        assert f"## {block.name}" in checklist
    for c in rubric.scored_criteria:
        assert f"{c.id}. {c.text}" in checklist
    assert "ДА(1):" in checklist
    assert "НЕТ(0):" in checklist


def test_prompt_marks_na_rules() -> None:
    """Elements with a Н.П. rule show it; others say it does not apply."""
    rubric = load_rubric()
    checklist = _checklist(_user(build_messages("[AGENT] привет", rubric, "outbound")))

    assert "Н.П.: Способ оплаты — не ипотека" in checklist  # item 17
    assert "Н.П.: не применяется" in checklist  # a mandatory element
    # The objections block carries its whole-block Н.П. note.
    assert "весь блок — Н.П., если возражений не было" in checklist


def test_prompt_category_is_not_weighted() -> None:
    """client_category no longer changes the checklist (v4 dropped weighting)."""
    rubric = load_rubric()
    with_cat = _checklist(_user(build_messages("[AGENT] x", rubric, "outbound", "B")))
    no_cat = _checklist(_user(build_messages("[AGENT] x", rubric, "outbound", None)))

    assert with_cat == no_cat
    assert "Категория клиента" not in with_cat
