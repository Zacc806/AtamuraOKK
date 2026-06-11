"""Provider-agnostic meeting-scoring interface + the data the scorer needs.

Consumers depend only on :class:`Scorer`, so the engine (Anthropic Claude Sonnet
by default, or any future provider) can be swapped in one place. The dataclasses
here are plain values — no DB or audio coupling — so the scorer is reusable and
trivially testable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class CallForScoring:
    """Minimal call data the scorer needs (decoupled from the DB and audio).

    The service layer builds this from a ``calls`` + ``transcripts`` row.
    """

    text: str  # speaker-tagged transcript ("[agent] ... [customer] ...")
    duration_sec: int
    language: str = "auto"  # detected language: "ru" | "kk" | "auto"
    language_probability: float = 1.0
    call_ref: str = ""  # opaque id, for logging only
    # Position of this contact in the client's chain (ТЗ 2.4): 1 = first, 2+ =
    # repeat. Lets the scorer apply repeat-visit leniency from CRM metadata, not
    # just the LLM's guess.
    visit_index: int = 1


@dataclass(slots=True)
class CriterionScore:
    """Score awarded for a single rubric criterion."""

    id: int
    block: str
    name: str
    score: int
    max_score: int
    auto: bool = False  # True if filled by an auto_check rule, not the LLM


@dataclass(slots=True)
class ScoreResult:
    """Full quality-control score for one call (matches the ``scores`` table)."""

    rubric_version: str
    total_score: int  # sum of all criterion scores (0..max_total)
    max_total: int  # rubric max_total_score (100)
    score_pct: float  # round(total_score / max_total * 100, 1)
    passed: bool  # score_pct >= pass_threshold
    criteria: list[CriterionScore]
    call_type: str  # первичный | повторный | уточняющий | сервисный
    client_agreed_meeting: bool
    manager_tone: str  # "вежливый" | "нейтральный" | "грубый" | "неуверенный"
    red_flags: list[str]
    summary: str
    language: str  # detected language: "ru" | "kk" | "shala"
    provider: str  # "anthropic"
    model: str
    needs_human_review: bool = False
    # Script-adherence dimension (None when no sales script is configured).
    script_adherence: float | None = None  # 0-100: how well the manager followed it
    script_deviations: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form (matches the ``scores`` table shape)."""
        return asdict(self)


@runtime_checkable
class Scorer(Protocol):
    """Turns a transcribed call into a rubric score."""

    async def score(self, call: CallForScoring) -> ScoreResult:
        """Score one call against the scorer's configured rubric.

        :param call: the transcript text + metadata to score.
        :returns: the structured score result.
        """
        ...
