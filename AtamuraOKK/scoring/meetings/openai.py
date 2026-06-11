"""OpenAI scorer — alternate meeting scorer (transport only).

Reuses the shared :class:`BaseLLMScorer` machinery (prompt build, retry, parse,
assemble); only the transport differs (OpenAI Chat Completions). Opt-in via
``meetings_scoring_engine="openai"`` — the default stays Anthropic Claude. Handy
when the Anthropic key is out of credit but the OpenAI key still has balance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from AtamuraOKK.scoring.meetings.errors import ProviderUnavailableError, ScoringError
from AtamuraOKK.scoring.meetings.llm import BaseLLMScorer
from AtamuraOKK.scoring.meetings.rubric import Rubric
from AtamuraOKK.scoring.meetings.script import Script

if TYPE_CHECKING:
    from openai import AsyncOpenAI

_HTTP_TOO_MANY = 429
_HTTP_SERVER_ERROR = 500
_MAX_OUTPUT_TOKENS = 1500


class OpenAIScorer(BaseLLMScorer):
    """Score a call with an OpenAI chat model returning JSON."""

    provider = "openai"

    def __init__(
        self,
        rubric: Rubric,
        *,
        api_key: str = "",
        model: str = "gpt-4o",
        client: AsyncOpenAI | None = None,
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

    def _ensure_client(self) -> AsyncOpenAI:
        if self._client is None:
            from openai import AsyncOpenAI  # noqa: PLC0415

            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def _raw_complete(self, prompt: str) -> str:
        from openai import (  # noqa: PLC0415
            APIConnectionError,
            APIStatusError,
            RateLimitError,
        )

        client = self._ensure_client()
        try:
            resp = await client.chat.completions.create(
                model=self.model,
                temperature=0,
                max_tokens=_MAX_OUTPUT_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
        except RateLimitError as exc:
            # Out-of-credit ("insufficient_quota") is permanent, not a transient
            # rate-limit — fail fast instead of burning the retry budget on it.
            if getattr(exc, "code", None) == "insufficient_quota":
                raise ScoringError(f"openai: {exc}") from exc
            raise ProviderUnavailableError(f"openai: {exc}") from exc
        except APIConnectionError as exc:
            raise ProviderUnavailableError(f"openai: {exc}") from exc
        except APIStatusError as exc:
            transient = (
                exc.status_code == _HTTP_TOO_MANY
                or exc.status_code >= _HTTP_SERVER_ERROR
            )
            if transient:
                raise ProviderUnavailableError(f"openai: {exc}") from exc
            raise ScoringError(f"openai: {exc}") from exc

        choices = resp.choices
        text = (choices[0].message.content or "").strip() if choices else ""
        if not text:
            raise ScoringError("openai: empty response")
        return text
