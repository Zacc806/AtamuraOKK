"""Groq scorer (Russian calls): Llama-3.3-70b via Groq's OpenAI-compatible API.

Ported from the legacy ``compliance_checker`` (same model, JSON mode, retry),
adapted to async and the :class:`BaseLLMScorer` retry loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from AtamuraOKK.scoring.errors import ProviderUnavailableError, ScoringError
from AtamuraOKK.scoring.llm import BaseLLMScorer
from AtamuraOKK.scoring.rubric import Rubric

if TYPE_CHECKING:
    from groq import AsyncGroq


class GroqScorer(BaseLLMScorer):
    """Score a call with a Groq chat model returning JSON."""

    provider = "groq"

    def __init__(
        self,
        rubric: Rubric,
        *,
        api_key: str = "",
        model: str = "llama-3.3-70b-versatile",
        client: AsyncGroq | None = None,
        max_retries: int = 5,
        retry_base_delay: float = 1.0,
        max_transcript_chars: int = 24000,
        pass_threshold: int = 75,
    ) -> None:
        super().__init__(
            rubric,
            model=model,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
            max_transcript_chars=max_transcript_chars,
            pass_threshold=pass_threshold,
        )
        self._api_key = api_key
        self._client = client

    def _ensure_client(self) -> AsyncGroq:
        if self._client is None:
            from groq import AsyncGroq  # noqa: PLC0415

            self._client = AsyncGroq(api_key=self._api_key)
        return self._client

    async def _raw_complete(self, prompt: str) -> str:
        from groq import (  # noqa: PLC0415
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
        )

        client = self._ensure_client()
        try:
            resp = await client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=900,
                response_format={"type": "json_object"},
            )
        except (
            RateLimitError,
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
        ) as exc:
            raise ProviderUnavailableError(f"groq: {exc}") from exc

        content: Any = resp.choices[0].message.content
        if not content:
            raise ScoringError("groq: empty response content")
        return str(content)
