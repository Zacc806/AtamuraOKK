"""Scorer factory: the single :class:`Scorer` seam the pipeline depends on.

Scoring runs on Anthropic Claude Sonnet, which handles Russian + Kazakh in one
model — so no per-language scorer routing is needed (unlike transcription, where
Kazakh is escalated to Yandex SpeechKit). Swapping the provider is a change to
:func:`build_scorer` and nothing else.
"""

from __future__ import annotations

from AtamuraOKK.scoring.anthropic import AnthropicScorer
from AtamuraOKK.scoring.base import Scorer
from AtamuraOKK.scoring.rubric import Rubric, load_rubric
from AtamuraOKK.scoring.script import load_script


def build_scorer(rubric: Rubric | None = None) -> Scorer:
    """Build the configured default scorer (Anthropic Claude Sonnet)."""
    from AtamuraOKK.settings import settings  # noqa: PLC0415

    rb = rubric or load_rubric(settings.score_rubric_version)
    script = load_script(settings.score_script_version)
    return AnthropicScorer(
        rb,
        api_key=settings.anthropic_api_key,
        model=settings.anthropic_model,
        max_retries=settings.score_max_retries,
        retry_base_delay=settings.score_retry_base_delay,
        max_transcript_chars=settings.score_max_transcript_chars,
        pass_threshold=settings.score_pass_threshold,
        script=script,
    )
