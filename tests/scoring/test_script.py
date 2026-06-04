"""Tests for the sales-script config + the script dimension in scoring."""

from __future__ import annotations

from AtamuraOKK.scoring.base import CallForScoring
from AtamuraOKK.scoring.prompts import build_prompt
from AtamuraOKK.scoring.result import assemble_score
from AtamuraOKK.scoring.rubric import load_rubric
from AtamuraOKK.scoring.schema import LLMScore
from AtamuraOKK.scoring.script import load_script

RUBRIC = load_rubric("okk_meeting_v1")


def test_no_script_version_returns_none() -> None:
    """Empty version disables the script dimension."""
    assert load_script("") is None


def test_unknown_script_returns_none() -> None:
    """An unknown script id returns None (dimension skipped, no crash)."""
    assert load_script("does_not_exist") is None


def test_example_script_loads() -> None:
    """The template script loads with ordered steps."""
    script = load_script("example_tm_call")
    assert script is not None
    assert len(script.steps) >= 5
    assert script.steps[0].id == 1


def test_prompt_includes_script_only_when_present() -> None:
    """The script section + fields appear in the prompt only when a script is given."""
    script = load_script("example_tm_call")
    with_script = build_prompt(
        RUBRIC,
        text="привет",
        duration_sec=60,
        max_chars=1000,
        script=script,
    )
    without = build_prompt(RUBRIC, text="привет", duration_sec=60, max_chars=1000)
    assert "СКРИПТ ПРОДАЖ" in with_script
    assert "script_adherence" in with_script
    assert "СКРИПТ ПРОДАЖ" not in without
    assert "script_adherence" not in without


def test_assemble_passes_and_clamps_script_adherence() -> None:
    """Script fields flow into ScoreResult; adherence is clamped to 0-100."""
    llm = LLMScore(
        scores={},
        script_adherence=150,
        script_deviations=["пропустил презентацию"],
    )
    result = assemble_score(
        llm,
        rubric=RUBRIC,
        call=CallForScoring(text="x", duration_sec=60, language="ru"),
        language="ru",
        provider="anthropic",
        model="m",
        pass_threshold=75,
    )
    assert result.script_adherence == 100.0
    assert result.script_deviations == ["пропустил презентацию"]


def test_no_script_leaves_adherence_none() -> None:
    """Without a script the adherence stays None and deviations empty."""
    result = assemble_score(
        LLMScore(scores={}),
        rubric=RUBRIC,
        call=CallForScoring(text="x", duration_sec=60, language="ru"),
        language="ru",
        provider="anthropic",
        model="m",
        pass_threshold=75,
    )
    assert result.script_adherence is None
    assert result.script_deviations == []
