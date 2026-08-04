"""Anthropic (Claude) scorer — the default production scorer.

Reuses the shared :class:`BaseLLMScorer` machinery (prompt build, retry, parse,
assemble); only the transport differs (Anthropic Messages API). Claude handles
Russian and Kazakh in one model, so scoring needs no language routing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from AtamuraOKK.scoring.meetings.config import config
from AtamuraOKK.scoring.meetings.errors import ProviderUnavailableError, ScoringError
from AtamuraOKK.scoring.meetings.llm import BaseLLMScorer
from AtamuraOKK.scoring.meetings.prompts import MeetingPrompt
from AtamuraOKK.scoring.meetings.rubric import Rubric
from AtamuraOKK.scoring.meetings.script import Script

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from anthropic.types import TextBlockParam

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

    def _content(self, prompt: MeetingPrompt) -> list[TextBlockParam]:
        """Prompt as content blocks, with the cache breakpoint after the framing.

        The framing is ~2.5k tokens and identical for every meeting on this
        rubric, so caching it serves ~40% of a typical request at ~0.1x. It clears
        the 1024-token minimum comfortably; the transcript stays after the
        breakpoint where it belongs.
        """
        framing: TextBlockParam = {"type": "text", "text": prompt.framing}
        if config.score_prompt_cache:
            framing["cache_control"] = {
                "type": "ephemeral",
                "ttl": config.score_cache_ttl,
            }
        return [framing, {"type": "text", "text": prompt.task}]

    async def _raw_complete(self, prompt: MeetingPrompt) -> str:
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
                messages=[{"role": "user", "content": self._content(prompt)}],
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

        if usage := getattr(resp, "usage", None):
            logger.debug(
                "meeting scoring usage: in={i} cache_write={w} cache_read={r} out={o}",
                i=usage.input_tokens,
                w=getattr(usage, "cache_creation_input_tokens", 0),
                r=getattr(usage, "cache_read_input_tokens", 0),
                o=usage.output_tokens,
            )
        text = "".join(
            getattr(block, "text", "")
            for block in resp.content
            if getattr(block, "type", None) == "text"
        ).strip()
        if not text:
            raise ScoringError("anthropic: empty response")
        return text
