"""Tests for LLM-output parsing/validation."""

from __future__ import annotations

import pytest

from AtamuraOKK.scoring.meetings.errors import MalformedOutputError
from AtamuraOKK.scoring.meetings.schema import parse_llm_json

_VALID = (
    '{"scores": {"1": 1, "2": 0}, "client_agreed_meeting": true, '
    '"manager_tone": "вежливый", "red_flags_found": [], "summary": "ok"}'
)


def test_parses_clean_json() -> None:
    """A clean JSON payload maps onto LLMScore fields."""
    result = parse_llm_json(_VALID)
    assert result.scores == {"1": 1, "2": 0}
    assert result.client_agreed_meeting is True
    assert result.manager_tone == "вежливый"


def test_strips_markdown_fences() -> None:
    """Markdown code fences around the JSON are tolerated."""
    fenced = f"```json\n{_VALID}\n```"
    assert parse_llm_json(fenced).scores == {"1": 1, "2": 0}


def test_extracts_json_amid_prose() -> None:
    """A JSON object embedded in chatty prose is extracted."""
    chatty = f"Конечно! Вот оценка:\n{_VALID}\nГотово."
    assert parse_llm_json(chatty).summary == "ok"


def test_defaults_for_optional_fields() -> None:
    """Optional fields default sensibly when omitted."""
    result = parse_llm_json('{"scores": {"1": 1}}')
    assert result.client_agreed_meeting is False
    assert result.red_flags_found == []


def test_no_json_raises() -> None:
    """Text with no JSON object raises MalformedOutputError."""
    with pytest.raises(MalformedOutputError):
        parse_llm_json("no json here at all")


def test_invalid_json_raises() -> None:
    """Syntactically broken JSON raises MalformedOutputError."""
    with pytest.raises(MalformedOutputError):
        parse_llm_json('{"scores": {"1": 1,, }}')


def test_wrong_scores_type_raises() -> None:
    """A non-dict scores field fails schema validation."""
    with pytest.raises(MalformedOutputError):
        parse_llm_json('{"scores": [1, 2, 3]}')
