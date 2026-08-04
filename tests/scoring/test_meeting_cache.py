"""Prompt-cache split for the ОП meeting scorer.

The saving depends on the framing staying byte-identical across meetings, so
these guard the two ways that breaks: per-meeting content drifting into the
framing, and the breakpoint landing on the wrong block.
"""

from __future__ import annotations

import pytest

from AtamuraOKK.scoring.meetings.anthropic import AnthropicScorer
from AtamuraOKK.scoring.meetings.config import config
from AtamuraOKK.scoring.meetings.prompts import build_prompt, build_prompt_parts
from AtamuraOKK.scoring.meetings.rubric import load_rubric

RUBRIC = load_rubric("okk_meeting_v1")


def test_framing_is_identical_across_meetings() -> None:
    """Different transcript, duration and visit index — same cacheable framing."""
    a = build_prompt_parts(
        RUBRIC, text="[agent] один", duration_sec=600, max_chars=1000
    )
    b = build_prompt_parts(
        RUBRIC, text="[agent] два", duration_sec=5400, max_chars=1000, visit_index=4
    )

    assert a.framing == b.framing
    assert a.task != b.task


def test_visit_context_sits_after_the_breakpoint() -> None:
    """A repeat visit must not fork the cache — this was the bug in the flat prompt."""
    first = build_prompt_parts(RUBRIC, text="x", duration_sec=60, max_chars=100)
    repeat = build_prompt_parts(
        RUBRIC, text="x", duration_sec=60, max_chars=100, visit_index=3
    )

    assert first.framing == repeat.framing
    assert "КОНТЕКСТ КЛИЕНТА" not in repeat.framing
    assert "КОНТЕКСТ КЛИЕНТА" in repeat.task
    assert "3-й" in repeat.task


def test_transcript_and_duration_stay_in_the_task() -> None:
    """Per-meeting content never leaks into the cached prefix."""
    parts = build_prompt_parts(
        RUBRIC, text="[agent] уникальная реплика", duration_sec=1234, max_chars=24000
    )

    assert "уникальная реплика" not in parts.framing
    assert "уникальная реплика" in parts.task
    assert "1234" not in parts.framing
    assert "1234" in parts.task


def test_flat_prompt_still_contains_everything() -> None:
    """build_prompt keeps its old contract for the OpenAI transport and tests."""
    flat = build_prompt(
        RUBRIC, text="[agent] привет", duration_sec=900, max_chars=24000, visit_index=2
    )

    assert "ЧЕК-ЛИСТ:" in flat
    assert "КОНТЕКСТ КЛИЕНТА" in flat
    assert "Транскрипция:" in flat
    assert "[agent] привет" in flat


def test_breakpoint_is_on_the_framing_block() -> None:
    """One breakpoint, on the framing; the transcript block carries none."""
    scorer = AnthropicScorer(RUBRIC)
    framing, task = scorer._content(  # noqa: SLF001
        build_prompt_parts(RUBRIC, text="x", duration_sec=60, max_chars=100),
    )

    assert framing["cache_control"] == {
        "type": "ephemeral",
        "ttl": config.score_cache_ttl,
    }
    assert "cache_control" not in task
    assert "ЧЕК-ЛИСТ:" in framing["text"]
    assert "Транскрипция:" in task["text"]


def test_cache_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The knob removes the breakpoint rather than changing its shape."""
    monkeypatch.setattr(config, "score_prompt_cache", False)
    scorer = AnthropicScorer(RUBRIC)
    framing, task = scorer._content(  # noqa: SLF001
        build_prompt_parts(RUBRIC, text="x", duration_sec=60, max_chars=100),
    )

    assert "cache_control" not in framing
    assert "cache_control" not in task
