"""Tests for the scoring cost levers: prompt caching + trimmed criterion output.

These guard the two properties the savings depend on: the cached prefix must stay
byte-identical across calls (any per-call byte before the breakpoint invalidates
it), and a passing criterion must be valid without its text fields.
"""

from __future__ import annotations

from typing import Any

import pytest

from AtamuraOKK.scoring.anthropic_scorer import build_request
from AtamuraOKK.scoring.base import CallScore, CriterionScore
from AtamuraOKK.scoring.batch import _call_id, _custom_id
from AtamuraOKK.scoring.prompt import build_prompt
from AtamuraOKK.scoring.rubric import load_rubric
from AtamuraOKK.settings import settings

RUBRIC = load_rubric()


def _request(transcript: str, direction: str = "outbound") -> dict[str, Any]:
    return build_request(
        transcript=transcript,
        rubric=RUBRIC,
        direction=direction,
        client_category=None,
        model="claude-sonnet-4-6",
        max_tokens=8000,
    )


def _blocks(request: dict[str, Any]) -> list[dict[str, Any]]:
    content = request["messages"][0]["content"]
    assert isinstance(content, list)
    return content


def test_stable_prefix_is_identical_across_calls() -> None:
    """System + checklist don't vary per call — that is what makes them cacheable."""
    a = build_prompt("[AGENT] привет", RUBRIC, "outbound")
    b = build_prompt("[AGENT] совсем другой разговор", RUBRIC, "inbound", "B")

    assert a.system == b.system
    assert a.checklist == b.checklist
    assert a.task != b.task


def test_transcript_and_direction_stay_after_the_breakpoint() -> None:
    """Per-call content must not leak into the cached prefix."""
    prompt = build_prompt("[AGENT] уникальная реплика", RUBRIC, "inbound")

    assert "уникальная реплика" not in prompt.system
    assert "уникальная реплика" not in prompt.checklist
    assert "уникальная реплика" in prompt.task
    # Direction varies per call, so it belongs after the breakpoint too.
    assert "входящий" in prompt.task
    assert "входящий" not in prompt.checklist


def test_cache_breakpoint_sits_on_the_checklist_block() -> None:
    """One breakpoint, on the last stable block, covering tools + system + checklist."""
    request = _request("[AGENT] привет")
    checklist, task = _blocks(request)

    assert checklist["cache_control"] == {
        "type": "ephemeral",
        "ttl": settings.anthropic_cache_ttl,
    }
    assert "cache_control" not in task
    assert "ЧЕК-ЛИСТ" in checklist["text"]
    assert "ТРАНСКРИПТ" in task["text"]


def test_cache_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The knob removes the breakpoint entirely rather than changing its shape."""
    monkeypatch.setattr(settings, "anthropic_prompt_cache", False)
    checklist, task = _blocks(_request("[AGENT] привет"))

    assert "cache_control" not in checklist
    assert "cache_control" not in task


def test_request_still_forces_the_scoring_tool() -> None:
    """Structured output must survive the caching refactor."""
    request = _request("[AGENT] привет")

    assert request["tool_choice"] == {"type": "tool", "name": "record_call_score"}
    assert request["tools"][0]["name"] == "record_call_score"
    assert request["temperature"] == 0


def test_passing_criterion_needs_no_text() -> None:
    """A ДА verdict validates with the three text fields omitted."""
    criterion = CriterionScore.model_validate({"id": 1, "score": 1})

    assert criterion.justification == ""
    assert criterion.evidence == ""
    assert criterion.recommendation == ""
    assert criterion.applicable is True


def test_failing_criterion_keeps_its_text() -> None:
    """A НЕТ verdict still carries the explanation the report renders."""
    criterion = CriterionScore.model_validate(
        {
            "id": 2,
            "score": 0,
            "justification": "не представился",
            "evidence": "алло",
            "recommendation": "назвать имя и компанию",
        },
    )

    assert criterion.justification == "не представился"
    assert criterion.recommendation == "назвать имя и компанию"


def test_score_schema_marks_criterion_text_optional() -> None:
    """The tool schema must not require the trimmed fields, or they come back."""
    schema = CallScore.model_json_schema()
    criterion = schema["$defs"]["CriterionScore"]
    required = set(criterion.get("required", []))

    assert "id" in required
    assert "score" in required
    assert not required & {"justification", "evidence", "recommendation"}


@pytest.mark.parametrize("call_id", [1, 42, 999999])
def test_batch_custom_id_round_trips(call_id: int) -> None:
    """Batch results are matched back to calls purely by custom_id."""
    assert _call_id(_custom_id(call_id)) == call_id


def test_batch_custom_id_rejects_foreign_ids() -> None:
    """An unrecognized custom_id is reported, not silently coerced to a call."""
    assert _call_id("meeting-7") is None
    assert _call_id("call-abc") is None
