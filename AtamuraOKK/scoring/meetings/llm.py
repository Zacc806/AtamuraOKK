"""Shared LLM-scorer machinery: prompt build, retry loop, parse, assemble.

Concrete providers (:class:`AnthropicScorer`) implement only the transport
(:meth:`_raw_complete`); this base owns the deterministic pipeline — build the
prompt, call the model, retry transient/malformed failures with exponential
backoff, then assemble the validated result.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod

from loguru import logger

from AtamuraOKK.scoring.meetings.base import CallForScoring, ScoreResult
from AtamuraOKK.scoring.meetings.cleanup import clean_transcript
from AtamuraOKK.scoring.meetings.errors import (
    MalformedOutputError,
    ProviderUnavailableError,
)
from AtamuraOKK.scoring.meetings.prompts import build_prompt
from AtamuraOKK.scoring.meetings.result import assemble_score
from AtamuraOKK.scoring.meetings.rubric import Rubric
from AtamuraOKK.scoring.meetings.schema import parse_llm_json
from AtamuraOKK.scoring.meetings.script import Script


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
        cleaned = clean_transcript(call.text)
        # The prompt builder cuts at max_chars as a last-resort cost guard. The
        # chunking layer (MeetingScorer) is supposed to keep inputs under the
        # cap, so an actual cut means lost content — score it, but say so and
        # flag the result for human review rather than fail silently.
        truncated_chars = max(0, len(cleaned) - self.max_transcript_chars)
        if truncated_chars:
            logger.warning(
                "scorer {p}: transcript for {ref} exceeds the {cap}-char cap "
                "by {cut} chars — truncated, flagging needs_human_review",
                p=self.provider,
                ref=call.call_ref or "<no ref>",
                cap=self.max_transcript_chars,
                cut=truncated_chars,
            )
        prompt = build_prompt(
            self.rubric,
            text=cleaned,
            duration_sec=call.duration_sec,
            max_chars=self.max_transcript_chars,
            script=self.script,
            visit_index=call.visit_index,
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

            meta: dict[str, object] = {
                "attempts": attempt,
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
            if truncated_chars:
                meta["truncated_chars"] = truncated_chars
            result = assemble_score(
                llm,
                rubric=self.rubric,
                call=call,
                language=call.language,
                provider=self.provider,
                model=self.model,
                pass_threshold=self.pass_threshold,
                kev_bonus_points=self.kev_bonus_points,
                meta=meta,
            )
            if truncated_chars:
                result.needs_human_review = True
            return result

        raise MalformedOutputError("retries exhausted")  # pragma: no cover
