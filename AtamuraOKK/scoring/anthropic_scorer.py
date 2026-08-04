"""Anthropic (Claude) implementation of :class:`Scorer`.

Claude has no native "response_format=schema", so we get structured output by
forcing a single tool call whose ``input_schema`` is the :class:`CallScore` JSON
schema, then validate the tool input back into the model. Temperature 0 for
consistent scoring.

:func:`build_request` and :func:`parse_response` are shared with the Batch API
path (``scoring/batch.py``) so both send byte-identical prompts — the batch path
would otherwise miss the prompt cache the realtime path just wrote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from AtamuraOKK.scoring.base import CallScore
from AtamuraOKK.scoring.prompt import build_prompt
from AtamuraOKK.settings import settings

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

    from AtamuraOKK.scoring.rubric import Rubric

_TOOL_NAME = "record_call_score"


def _inline_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve ``$ref``/``$defs`` into a self-contained schema.

    Pydantic emits nested models (CriterionScore) as ``$defs`` + ``$ref``; inlining
    avoids any ambiguity in how the tool schema is interpreted.
    """
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                merged = resolve(dict(defs.get(name, {})))
                for key, val in node.items():
                    if key != "$ref":
                        merged[key] = resolve(val)
                return merged
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)


def _tool() -> dict[str, Any]:
    """The forced tool whose input schema *is* :class:`CallScore`."""
    return {
        "name": _TOOL_NAME,
        "description": "Сохрани структурированную оценку звонка по чек-листу ОКК.",
        "input_schema": _inline_defs(CallScore.model_json_schema()),
    }


def build_request(
    *,
    transcript: str,
    rubric: Rubric,
    direction: str,
    client_category: str | None,
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    """Build the ``messages.create`` parameters for one call.

    The cache breakpoint sits on the checklist block, which is the last of the
    stable content: rendering order is tools -> system -> messages, so one
    ``cache_control`` there covers the tool schema, the system prompt and the
    checklist (~5.8k tokens) while the per-call transcript stays after it.
    """
    prompt = build_prompt(transcript, rubric, direction, client_category)
    checklist: dict[str, Any] = {"type": "text", "text": prompt.checklist}
    if settings.anthropic_prompt_cache:
        checklist["cache_control"] = {
            "type": "ephemeral",
            "ttl": settings.anthropic_cache_ttl,
        }
    return {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0,
        "system": prompt.system,
        "messages": [
            {
                "role": "user",
                "content": [checklist, {"type": "text", "text": prompt.task}],
            },
        ],
        "tools": [_tool()],
        "tool_choice": {"type": "tool", "name": _TOOL_NAME},
    }


def parse_response(content: list[Any], stop_reason: str | None) -> CallScore:
    """Validate the forced tool call out of a Claude response.

    A truncated response is rejected rather than parsed: the tool input would be
    incomplete JSON, which either raises here or silently drops criteria that the
    worker would then score 0, deflating the call.
    """
    if stop_reason == "max_tokens":
        raise RuntimeError(
            "Anthropic scoring truncated (stop_reason=max_tokens); "
            "raise ATAMURAOKK_ANTHROPIC_MAX_TOKENS.",
        )
    for block in content:
        if block.type == "tool_use" and block.name == _TOOL_NAME:
            parsed = CallScore.model_validate(block.input)
            logger.debug("Scored transcript: {n} criteria", n=len(parsed.criteria))
            return parsed
    raise RuntimeError(
        f"Anthropic scorer returned no tool_use (stop_reason={stop_reason})",
    )


class AnthropicScorer:
    """Score a transcript with Claude via a forced structured tool call."""

    def __init__(self, model: str | None = None, *, api_key: str | None = None) -> None:
        self.model = model or settings.anthropic_scoring_model
        self._api_key = api_key or settings.anthropic_api_key
        self._client: AsyncAnthropic | None = None

    @property
    def model_label(self) -> str:
        """Provider-prefixed model id stored on the score."""
        return f"anthropic/{self.model}"

    def _get_client(self) -> AsyncAnthropic:
        if self._client is None:
            from anthropic import AsyncAnthropic  # noqa: PLC0415

            if not self._api_key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set (ATAMURAOKK_ANTHROPIC_API_KEY).",
                )
            self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def score(
        self,
        *,
        transcript: str,
        rubric: Rubric,
        direction: str,
        client_category: str | None = None,
    ) -> CallScore:
        """Return the structured QA assessment for one call."""
        client = self._get_client()
        params = build_request(
            transcript=transcript,
            rubric=rubric,
            direction=direction,
            client_category=client_category,
            model=self.model,
            max_tokens=settings.anthropic_max_tokens,
        )
        resp = await client.messages.create(**params)  # type: ignore[call-overload]
        if usage := getattr(resp, "usage", None):
            logger.debug(
                "scoring usage: in={i} cache_write={w} cache_read={r} out={o}",
                i=usage.input_tokens,
                w=getattr(usage, "cache_creation_input_tokens", 0),
                r=getattr(usage, "cache_read_input_tokens", 0),
                o=usage.output_tokens,
            )
        return parse_response(resp.content, resp.stop_reason)
