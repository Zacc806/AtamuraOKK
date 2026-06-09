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
from AtamuraOKK.scoring.meetings.disk import (
    BitrixDisk,
    MeetingDiskSource,
    MeetingFile,
)
from AtamuraOKK.scoring.meetings.download import download_pending
from AtamuraOKK.scoring.meetings.errors import (
    MalformedOutputError,
    ProviderUnavailableError,
    ScoringError,
)
from AtamuraOKK.scoring.meetings.llm import BaseLLMScorer
from AtamuraOKK.scoring.meetings.meeting import MeetingScorer
from AtamuraOKK.scoring.meetings.openai import OpenAIScorer
from AtamuraOKK.scoring.meetings.recordings import (
    drain_pipeline,
    ingest_recordings,
    requeue_failed,
    run_pipeline,
    score_pending,
)
from AtamuraOKK.scoring.meetings.router import build_meeting_scorer
from AtamuraOKK.scoring.meetings.rubric import Criterion, Rubric, load_rubric
from AtamuraOKK.scoring.meetings.script import Script, load_script
from AtamuraOKK.scoring.meetings.store import MeetingStatus, MeetingStore
from AtamuraOKK.scoring.meetings.transcribe import transcribe_pending

__all__ = [
    "AnthropicScorer",
    "BaseLLMScorer",
    "BitrixDisk",
    "CallForScoring",
    "Criterion",
    "CriterionScore",
    "MalformedOutputError",
    "MeetingDiskSource",
    "MeetingFile",
    "MeetingScorer",
    "MeetingStatus",
    "MeetingStore",
    "OpenAIScorer",
    "ProviderUnavailableError",
    "Rubric",
    "ScoreResult",
    "Scorer",
    "ScoringError",
    "Script",
    "build_meeting_scorer",
    "chunk_transcript",
    "download_pending",
    "drain_pipeline",
    "ingest_recordings",
    "load_rubric",
    "load_script",
    "requeue_failed",
    "run_pipeline",
    "score_pending",
    "transcribe_pending",
]
