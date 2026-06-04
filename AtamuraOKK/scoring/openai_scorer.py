"""OpenAI implementation of :class:`Scorer` using Structured Outputs.

Returns a schema-validated :class:`CallScore` (the SDK enforces the JSON schema and
retries malformed generations). Temperature 0 for consistent scoring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from AtamuraOKK.scoring.base import CallScore
from AtamuraOKK.scoring.prompt import build_messages
from AtamuraOKK.settings import settings

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from AtamuraOKK.scoring.rubric import Rubric


class OpenAIScorer:
    """Score a transcript with an OpenAI Structured-Outputs model."""

    def __init__(
        self,
        model: str | None = None,
        *,
        api_key: str | None = None,
    ) -> None:
        self.model = model or settings.openai_scoring_model
        self._api_key = api_key or settings.openai_api_key
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            from openai import AsyncOpenAI  # noqa: PLC0415

            if not self._api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY is not set (ATAMURAOKK_OPENAI_API_KEY).",
                )
            self._client = AsyncOpenAI(api_key=self._api_key)
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
        completion = await client.beta.chat.completions.parse(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            response_format=CallScore,
            temperature=0,
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            refusal = completion.choices[0].message.refusal
            raise RuntimeError(f"Scorer returned no parsed output (refusal={refusal})")
        logger.debug("Scored transcript: {n} criteria", n=len(parsed.criteria))
        return parsed
