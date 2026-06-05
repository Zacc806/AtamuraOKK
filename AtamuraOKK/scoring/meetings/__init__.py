"""ОП-meeting quality-control scoring — standalone, parallel automation.

Independent of the call-scoring package: own config, own transcript cleanup, own
rubric. Scores an ОП meeting transcript against the okk_meeting_v1 checklist.
"""

from AtamuraOKK.scoring.meetings.anthropic import AnthropicScorer
from AtamuraOKK.scoring.meetings.base import (
    CallForScoring,
    CriterionScore,
    Scorer,
    ScoreResult,
)
from AtamuraOKK.scoring.meetings.chunking import chunk_transcript
from AtamuraOKK.scoring.meetings.errors import (
    MalformedOutputError,
    ProviderUnavailableError,
    ScoringError,
)
from AtamuraOKK.scoring.meetings.llm import BaseLLMScorer
from AtamuraOKK.scoring.meetings.meeting import MeetingScorer
from AtamuraOKK.scoring.meetings.router import build_meeting_scorer
from AtamuraOKK.scoring.meetings.rubric import Criterion, Rubric, load_rubric
from AtamuraOKK.scoring.meetings.script import Script, load_script

__all__ = [
    "AnthropicScorer",
    "BaseLLMScorer",
    "CallForScoring",
    "Criterion",
    "CriterionScore",
    "MalformedOutputError",
    "MeetingScorer",
    "ProviderUnavailableError",
    "Rubric",
    "ScoreResult",
    "Scorer",
    "ScoringError",
    "Script",
    "build_meeting_scorer",
    "chunk_transcript",
    "load_rubric",
    "load_script",
]
