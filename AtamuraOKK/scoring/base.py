"""Scorer interface + the structured result schema the LLM must return.

The pipeline depends only on :class:`Scorer`, so the scoring model can be swapped.
``CallScore`` is the validated structured output; the worker derives the numeric
total / percent / zone from it against the active rubric.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from AtamuraOKK.scoring.rubric import Rubric

Sentiment = Literal["позитивный", "нейтральный", "негативный"]
TargetStatus = Literal["целевой", "нецелевой", "неясно"]
# Type of conversation — the qualification checklist only applies to a genuine
# first-contact sales/qualification call. Everything else is excluded from the
# team score so reminders/vendor/internal/wrong-number calls don't distort it.
CallType = Literal[
    "квалификация",  # genuine sales/qualification call — score it
    "напоминание",  # appointment reminder to an already-booked client
    "повторный_сервисный",  # follow-up / service call, not a first qualification
    "вендор_или_спам",  # an outside party selling to us / spam
    "внутренний",  # staff talking to each other (e.g. audio test)
    "недозвон_или_ошибка",  # no real conversation / wrong number
    "другое",
]


class CriterionScore(BaseModel):
    """Score for one rubric criterion."""

    id: int = Field(description="Номер критерия из чек-листа")
    score: int = Field(description="Баллы за критерий (0..max)")
    justification: str = Field(description="Краткое обоснование оценки на русском")
    evidence: str = Field(
        description="Цитата из разговора на языке оригинала (или пусто)"
    )
    recommendation: str = Field(
        description="Конкретная рекомендация менеджеру: что улучшить по этому "
        "критерию на следующем звонке (на русском)"
    )


class CallScore(BaseModel):
    """Full structured QA assessment of one call (LLM output)."""

    call_type: CallType = Field(description="Тип звонка")
    is_qualification_call: bool = Field(
        description="Это настоящий квалификационный звонок ТМ, к которому применим "
        "чек-лист (False для напоминаний, вендоров, внутренних, недозвонов)",
    )
    manager_identified: bool = Field(
        description="Удалось ли однозначно определить менеджера Atamura в разговоре",
    )
    criteria: list[CriterionScore]
    objections_present: bool = Field(description="Были ли у клиента возражения")
    sentiment_customer: Sentiment
    sentiment_agent: Sentiment
    summary: str = Field(description="Резюме звонка в 2-3 предложениях на русском")
    red_flags: list[str] = Field(description="Список нарушений/красных флагов")
    target_status: TargetStatus = Field(
        description="Целевой ли клиент по итогам звонка"
    )
    strengths: str = Field(description="Сильные стороны менеджера")
    growth_zone: str = Field(description="Зона роста менеджера")
    training_recommendation: str = Field(description="Рекомендация по обучению")


@runtime_checkable
class Scorer(Protocol):
    """Scores a transcript against a rubric into a validated :class:`CallScore`."""

    @property
    def model_label(self) -> str:
        """Provider-prefixed model id stored on the score (e.g. ``anthropic/...``)."""
        ...

    async def score(
        self,
        *,
        transcript: str,
        rubric: Rubric,
        direction: str,
    ) -> CallScore:
        """Return the structured QA assessment for one call."""
        ...
