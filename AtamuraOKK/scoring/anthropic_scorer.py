"""Anthropic (Claude) implementation of :class:`Scorer`.

Claude has no native "response_format=schema", so we get structured output by
forcing a single tool call whose ``input_schema`` is the :class:`CallScore` JSON
schema, then validate the tool input back into the model. Temperature 0 for
consistent scoring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from AtamuraOKK.scoring.base import CallScore
from AtamuraOKK.scoring.prompt import build_messages
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
    ) -> CallScore:
        """Return the structured QA assessment for one call."""
        client = self._get_client()
        messages = build_messages(transcript, rubric, direction)
        system = next(m["content"] for m in messages if m["role"] == "system")
        user_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m["role"] != "system"
        ]
        tool = {
            "name": _TOOL_NAME,
            "description": "Сохрани структурированную оценку звонка по чек-листу ОКК.",
            "input_schema": _inline_defs(CallScore.model_json_schema()),
        }
        resp = await client.messages.create(  # type: ignore[call-overload]
            model=self.model,
            max_tokens=settings.anthropic_max_tokens,
            temperature=0,
            system=system,
            messages=user_messages,
            tools=[tool],
            tool_choice={"type": "tool", "name": _TOOL_NAME},
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == _TOOL_NAME:
                parsed = CallScore.model_validate(block.input)
                logger.debug("Scored transcript: {n} criteria", n=len(parsed.criteria))
                return parsed
        raise RuntimeError(
            f"Anthropic scorer returned no tool_use (stop_reason={resp.stop_reason})",
        )
