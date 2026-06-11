"""Meeting-scorer factory: the single :class:`Scorer` seam this automation uses.

Runs on Anthropic Claude Sonnet, which handles Russian + Kazakh in one model.
Standalone — does not touch the call-scoring automation.
"""

from __future__ import annotations

from AtamuraOKK.scoring.meetings.anthropic import AnthropicScorer
from AtamuraOKK.scoring.meetings.base import Scorer
from AtamuraOKK.scoring.meetings.config import MeetingScoringConfig
from AtamuraOKK.scoring.meetings.llm import BaseLLMScorer
from AtamuraOKK.scoring.meetings.meeting import MeetingScorer
from AtamuraOKK.scoring.meetings.openai import OpenAIScorer
from AtamuraOKK.scoring.meetings.rubric import Rubric, load_rubric
from AtamuraOKK.scoring.meetings.script import Script, load_script


def _build_base_scorer(
    settings: MeetingScoringConfig,
    rb: Rubric,
    script: Script | None,
) -> BaseLLMScorer:
    """Pick the per-chunk transport from ``meetings_scoring_engine``."""
    if settings.meetings_scoring_engine.lower() == "openai":
        return OpenAIScorer(
            rb,
            api_key=settings.openai_api_key,
            model=settings.openai_scoring_model,
            max_retries=settings.score_max_retries,
            retry_base_delay=settings.score_retry_base_delay,
            max_transcript_chars=settings.score_meeting_chunk_chars,
            pass_threshold=settings.score_pass_threshold,
            script=script,
        )
    return AnthropicScorer(
        rb,
        api_key=settings.anthropic_api_key,
        model=settings.anthropic_model,
        max_retries=settings.score_max_retries,
        retry_base_delay=settings.score_retry_base_delay,
        max_transcript_chars=settings.score_meeting_chunk_chars,
        pass_threshold=settings.score_pass_threshold,
        script=script,
    )


def build_meeting_scorer(rubric: Rubric | None = None) -> Scorer:
    """Build the ОП-meeting scorer (Этап 3): chunk long transcripts + map-reduce.

    The wrapped per-chunk scorer's transport (Anthropic by default, OpenAI when
    ``meetings_scoring_engine="openai"``) gets its transcript cap raised to the
    chunk size so chunks are never truncated.
    """
    from AtamuraOKK.scoring.meetings.config import config as settings  # noqa: PLC0415

    rb = rubric or load_rubric(settings.score_meeting_rubric_version)
    script = load_script(settings.score_script_version)
    base = _build_base_scorer(settings, rb, script)
    return MeetingScorer(
        base,
        rubric=rb,
        chunk_chars=settings.score_meeting_chunk_chars,
        pass_threshold=settings.score_pass_threshold,
        overlap_lines=settings.score_meeting_overlap_lines,
    )
