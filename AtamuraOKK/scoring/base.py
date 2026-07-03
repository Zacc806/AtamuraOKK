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
# How the client intends to pay, as stated/confirmed on the call. Drives the
# cash-buyer manager alert (наличные); «неизвестно» when not discussed.
PaymentMethod = Literal["наличные", "ипотека", "рассрочка", "другое", "неизвестно"]
# Type of conversation — the qualification checklist only applies to a genuine
# first-contact sales/qualification call. Everything else is excluded from the
# team score so reminders/vendor/internal/wrong-number/non-client calls don't
# distort it (only «квалификация» counts as a real attempt to book into ОП).
CallType = Literal[
    "квалификация",  # genuine sales/qualification call — score it
    "напоминание",  # appointment reminder to an already-booked client
    "повторный_сервисный",  # follow-up / service call, not a first qualification
    "нецелевое_обращение",  # not a buyer: realtor/agent, job applicant, partner, etc.
    "вендор_или_спам",  # an outside party selling to us / commercial offer (КП) / spam
    "внутренний",  # staff talking to each other (e.g. audio test)
    "недозвон_или_ошибка",  # no real conversation / wrong number
    "другое",
]


class CriterionScore(BaseModel):
    """Binary verdict for one rubric element (ДА=1 / НЕТ=0 / Н.П.)."""

    id: int = Field(description="Номер элемента из чек-листа")
    score: int = Field(
        description="1 = ДА (элемент выполнен), 0 = НЕТ (не выполнен). "
        "При applicable=false значение игнорируется."
    )
    applicable: bool = Field(
        default=True,
        description="true = элемент применим и оценивается; false = Н.П. "
        "(неприменим к этому звонку) — элемент исключается из подсчёта. "
        "Ставь false ТОЛЬКО когда в чек-листе для этого элемента указано условие "
        "Н.П. и оно выполняется.",
    )
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
        description="Это настоящий квалификационный звонок ТМ клиенту-покупателю, к "
        "которому применим чек-лист. False для напоминаний, вендоров/КП, внутренних, "
        "недозвонов и НЕЦЕЛЕВЫХ обращений (риэлтор/агент, резюме, прочие не-клиенты)",
    )
    manager_identified: bool = Field(
        description="Удалось ли однозначно определить менеджера Atamura в разговоре",
    )
    # Which audio side the manager turned out to be on. Lets the pipeline reconcile
    # the stored transcript labels with the content-identified manager (the stereo
    # channel->role guess is sometimes inverted). "A" = СТОРОНА A (канал 1, метка
    # [AGENT]); "B" = СТОРОНА B (канал 2, метка [CUSTOMER]) -> labels were inverted.
    manager_side: Literal["A", "B", "unknown"] = Field(
        default="unknown",
        description="На какой СТОРОНЕ оказался менеджер Atamura: «A» — СТОРОНА A "
        "(аудиоканал 1), «B» — СТОРОНА B (аудиоканал 2); «unknown», если менеджер "
        "не определён (manager_identified=false) или запись в один канал",
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
    # Defaults harden the existing prompt: an omitted field can't fail validation.
    payment_method: PaymentMethod = Field(
        default="неизвестно",
        description="Способ оплаты, который клиент назвал/подтвердил "
        "(наличные/ипотека/рассрочка); «неизвестно», если не обсуждалось",
    )
    wants_to_visit: bool = Field(
        default=False,
        description="Клиент согласился приехать в офис / на показ квартир (КЭВ) "
        "или явно выразил такое намерение",
    )
    on_premises: bool = Field(
        default=False,
        description="Клиент уже находится в офисе / на объекте по словам в разговоре",
    )


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
        client_category: str | None = None,
    ) -> CallScore:
        """Return the structured QA assessment for one call."""
        ...
