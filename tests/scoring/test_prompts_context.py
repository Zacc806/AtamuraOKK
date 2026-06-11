"""Prompt seams: client history (2.4), diarization labels (3.1), nonverbal (3.3)."""

from __future__ import annotations

from AtamuraOKK.scoring.meetings.prompts import build_prompt
from AtamuraOKK.scoring.meetings.rubric import load_rubric

RUBRIC = load_rubric("okk_meeting_v1")


def _prompt(**kwargs: object) -> str:
    return build_prompt(RUBRIC, text="x", duration_sec=100, max_chars=100, **kwargs)  # type: ignore[arg-type]


def test_repeat_visit_adds_client_context() -> None:
    """ТЗ 2.4: a 2nd+ contact injects the repeat-visit leniency note."""
    prompt = _prompt(visit_index=3)
    assert "КОНТЕКСТ КЛИЕНТА (CRM)" in prompt
    assert "3-й" in prompt
    assert "повторный" in prompt


def test_first_visit_has_no_repeat_context() -> None:
    """A first contact adds no repeat note."""
    assert "КОНТЕКСТ КЛИЕНТА (CRM)" not in _prompt(visit_index=1)


def test_prompt_handles_diarization_labels() -> None:
    """ТЗ 3.1: the scorer is told how to read [speaker_N] diarization labels."""
    assert "speaker_1" in _prompt()


def test_prompt_consumes_nonverbal_cues() -> None:
    """ТЗ 3.3: the scorer uses pause/hesitation markup when present."""
    assert "[пауза" in _prompt()
