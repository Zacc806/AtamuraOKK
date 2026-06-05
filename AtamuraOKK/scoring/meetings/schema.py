"""Wire contract for the scoring LLM's JSON output + tolerant parsing.

The JSON shape is provider-independent, so the prompt and validation are
shared. Ported from the legacy ``compliance_checker`` response format.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from AtamuraOKK.scoring.meetings.errors import MalformedOutputError


class LLMScore(BaseModel):
    """The structured JSON the scoring LLM must return."""

    scores: dict[str, int]  # {criterion_id_as_str: awarded_points}
    # Call type (контекстный режим): первичный | повторный | уточняющий | сервисный.
    call_type: str = "первичный"
    client_agreed_meeting: bool = False
    manager_tone: str = "нейтральный"
    # Client emotional state (ТЗ 2.2): спокоен | спешит | раздражён | эмоционален.
    client_emotion: str = "спокоен"
    red_flags_found: list[str] = Field(default_factory=list)
    summary: str = ""
    # Present only when a sales script was supplied in the prompt.
    script_adherence: float | None = None
    script_deviations: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


_FENCE_RE = re.compile(r"^```(?:json)?\n?|\n?```$")
_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_llm_json(answer: str) -> LLMScore:
    """Parse and validate raw LLM text into an :class:`LLMScore`.

    Tolerates markdown code fences and surrounding prose, mirroring the legacy
    checker's robustness to chatty models.

    :param answer: raw text returned by the LLM.
    :returns: the validated structured score.
    :raises MalformedOutputError: if no valid JSON object can be extracted.
    """
    cleaned = _FENCE_RE.sub("", answer.strip())
    match = _OBJ_RE.search(cleaned)
    if not match:
        raise MalformedOutputError("no JSON object in LLM answer")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise MalformedOutputError(f"invalid JSON: {exc}") from exc
    try:
        return LLMScore.model_validate(data)
    except ValidationError as exc:
        raise MalformedOutputError(f"schema mismatch: {exc}") from exc
