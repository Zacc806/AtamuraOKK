"""Shared LLM-scorer machinery: prompt build, retry loop, parse, assemble.

Concrete providers (:class:`AnthropicScorer`) implement only the transport
(:meth:`_raw_complete`). The retry/backoff loop mirrors ``BitrixClient.call``
and the legacy ``compliance_checker.check_one``.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod

from loguru import logger

from AtamuraOKK.scoring.base import CallForScoring, ScoreResult
from AtamuraOKK.scoring.errors import MalformedOutputError, ProviderUnavailableError
from AtamuraOKK.scoring.prompts import build_prompt
from AtamuraOKK.scoring.result import assemble_score
from AtamuraOKK.scoring.rubric import Rubric
from AtamuraOKK.scoring.schema import parse_llm_json
from AtamuraOKK.scoring.script import Script
from AtamuraOKK.transcription.cleanup import clean_transcript


class BaseLLMScorer(ABC):
    """Provider-independent scoring: build prompt, call LLM, retry, assemble."""

    provider: str = "llm"

    def __init__(
        self,
        rubric: Rubric,
        *,
        model: str,
        max_retries: int = 5,
        retry_base_delay: float = 1.0,
        max_transcript_chars: int = 24000,
        pass_threshold: int = 75,
        script: Script | None = None,
        kev_bonus_points: int = 10,
    ) -> None:
        self.rubric = rubric
        self.model = model
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.max_transcript_chars = max_transcript_chars
        self.pass_threshold = pass_threshold
        self.script = script
        self.kev_bonus_points = kev_bonus_points

    @abstractmethod
    async def _raw_complete(self, prompt: str) -> str:
        """Send the prompt to the provider and return its raw text answer.

        :raises ProviderUnavailableError: on rate limit / network / 5xx.
        """

    async def score(self, call: CallForScoring) -> ScoreResult:
        """Score one call, retrying transient and malformed-output failures."""
        prompt = build_prompt(
            self.rubric,
            text=clean_transcript(call.text),
            duration_sec=call.duration_sec,
            max_chars=self.max_transcript_chars,
            script=self.script,
        )
        delay = self.retry_base_delay
        started = time.monotonic()

        for attempt in range(1, self.max_retries + 1):
            last = attempt == self.max_retries
            try:
                raw = await self._raw_complete(prompt)
                llm = parse_llm_json(raw)
            except (ProviderUnavailableError, MalformedOutputError) as exc:
                logger.warning(
                    "scorer {p} attempt {n}/{m} failed: {e}",
                    p=self.provider,
                    n=attempt,
                    m=self.max_retries,
                    e=exc,
                )
                if last:
                    raise
                await asyncio.sleep(delay)
                delay *= 2
                continue

            return assemble_score(
                llm,
                rubric=self.rubric,
                call=call,
                language=call.language,
                provider=self.provider,
                model=self.model,
                pass_threshold=self.pass_threshold,
                kev_bonus_points=self.kev_bonus_points,
                meta={
                    "attempts": attempt,
                    "latency_ms": round((time.monotonic() - started) * 1000),
                },
            )

        raise MalformedOutputError("retries exhausted")  # pragma: no cover
