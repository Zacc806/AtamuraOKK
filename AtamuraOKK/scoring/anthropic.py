"""Anthropic (Claude) scorer — the default production scorer.

Reuses the shared :class:`BaseLLMScorer` machinery (prompt build, retry, parse,
assemble); only the transport differs (Anthropic Messages API). Claude handles
Russian and Kazakh in one model, so scoring needs no language routing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from AtamuraOKK.scoring.errors import ProviderUnavailableError, ScoringError
from AtamuraOKK.scoring.llm import BaseLLMScorer
from AtamuraOKK.scoring.rubric import Rubric
from AtamuraOKK.scoring.script import Script

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

_HTTP_TOO_MANY = 429
_HTTP_SERVER_ERROR = 500
_MAX_OUTPUT_TOKENS = 1500


class AnthropicScorer(BaseLLMScorer):
    """Score a call with a Claude model returning JSON."""

    provider = "anthropic"

    def __init__(
        self,
        rubric: Rubric,
        *,
        api_key: str = "",
        model: str = "claude-sonnet-4-6",
        client: AsyncAnthropic | None = None,
        max_retries: int = 5,
        retry_base_delay: float = 1.0,
        max_transcript_chars: int = 24000,
        pass_threshold: int = 75,
        script: Script | None = None,
    ) -> None:
        super().__init__(
            rubric,
            model=model,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
            max_transcript_chars=max_transcript_chars,
            pass_threshold=pass_threshold,
            script=script,
        )
        self._api_key = api_key
        self._client = client

    def _ensure_client(self) -> AsyncAnthropic:
        if self._client is None:
            from anthropic import AsyncAnthropic  # noqa: PLC0415

            self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def _raw_complete(self, prompt: str) -> str:
        from anthropic import (  # noqa: PLC0415
            APIConnectionError,
            APIStatusError,
            RateLimitError,
        )

        client = self._ensure_client()
        try:
            resp = await client.messages.create(
                model=self.model,
                max_tokens=_MAX_OUTPUT_TOKENS,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
        except (RateLimitError, APIConnectionError) as exc:
            raise ProviderUnavailableError(f"anthropic: {exc}") from exc
        except APIStatusError as exc:
            transient = (
                exc.status_code == _HTTP_TOO_MANY
                or exc.status_code >= _HTTP_SERVER_ERROR
            )
            if transient:
                raise ProviderUnavailableError(f"anthropic: {exc}") from exc
            raise ScoringError(f"anthropic: {exc}") from exc

        text = "".join(
            getattr(block, "text", "")
            for block in resp.content
            if getattr(block, "type", None) == "text"
        ).strip()
        if not text:
            raise ScoringError("anthropic: empty response")
        return text
