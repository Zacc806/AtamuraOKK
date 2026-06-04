"""Tests for client-emotion capture (ТЗ 2.2)."""

from __future__ import annotations

from AtamuraOKK.scoring.base import CallForScoring
from AtamuraOKK.scoring.meeting import _peak_emotion
from AtamuraOKK.scoring.prompts import build_prompt
from AtamuraOKK.scoring.result import assemble_score
from AtamuraOKK.scoring.rubric import load_rubric
from AtamuraOKK.scoring.schema import LLMScore

RUBRIC = load_rubric("okk_meeting_v1")


def test_prompt_asks_for_client_emotion() -> None:
    """The prompt requests client_emotion and the empathy rule."""
    prompt = build_prompt(RUBRIC, text="x", duration_sec=100, max_chars=100)
    assert "client_emotion" in prompt
    assert "состояние клиента" in prompt


def test_assemble_records_client_emotion_in_meta() -> None:
    """assemble_score stores client_emotion in meta (no scores column needed)."""
    llm = LLMScore(scores={}, client_emotion="раздражён")
    result = assemble_score(
        llm,
        rubric=RUBRIC,
        call=CallForScoring(text="x", duration_sec=100, language="ru"),
        language="ru",
        provider="anthropic",
        model="m",
        pass_threshold=75,
    )
    assert result.meta["client_emotion"] == "раздражён"


def test_peak_emotion_picks_most_intense() -> None:
    """Across chunks the most intense known emotion wins; unknowns ignored."""
    assert _peak_emotion(["спокоен", "раздражён", "спешит"]) == "раздражён"
    assert _peak_emotion(["спокоен", "спокоен"]) == "спокоен"
    assert _peak_emotion(["мусор"]) == "спокоен"
    assert _peak_emotion([]) == "спокоен"
