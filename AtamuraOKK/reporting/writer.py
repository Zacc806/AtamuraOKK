"""LLM writer: turn aggregated report data into the ОКК narrative sections."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from AtamuraOKK.settings import settings

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from AtamuraOKK.reporting.aggregate import ReportData


class ManagerNote(BaseModel):
    """Consolidated per-manager assessment."""

    name: str
    strengths: str = Field(description="Сильные стороны менеджера за период")
    growth_zone: str = Field(description="Зона роста")
    training: str = Field(description="Рекомендация по обучению")


class ReportNarrative(BaseModel):
    """The written sections of a half-day ОКК report (all Russian)."""

    overall_assessment: str = Field(
        description="Общая оценка работы ТМ, 2-4 предложения"
    )
    manager_notes: list[ManagerNote]
    systemic_problems: list[str] = Field(description="Системные ошибки команды")
    recommendations: list[str] = Field(description="Задачи и рекомендации для РОПов")
    conclusion: str = Field(description="Краткий итог и прогноз")


_SYSTEM = """\
Ты — руководитель отдела контроля качества (ОКК) Atamura Group (продажа \
недвижимости, телемаркетинг/ТМ). Составь внутренний отчёт по качеству звонков ТМ \
за указанный период в стиле компании: по делу, с конкретикой, на русском.
Структура: общая оценка; по каждому менеджеру — сильные стороны, зона роста и \
рекомендация по обучению (на основе его звонков); системные ошибки команды; \
задачи для РОПов; краткий итог. Опирайся ТОЛЬКО на предоставленные данные.\
"""


def _data_block(data: ReportData) -> str:
    lines = [
        f"Период: {data.date_label}",
        f"Звонков оценено: {data.n_scored}; средний балл команды: "
        f"{data.avg_percent}% (норма {settings.report_score_norm}%+)",
        f"Зоны: {data.zones}; целевые/нецелевые: {data.targets}; "
        f"во флагах: {data.n_flagged}",
        f"Исключено звонков не по теме (не квалификация): {data.n_excluded} "
        f"({data.excluded_by_type})",
        "",
        "Менеджеры (по убыванию среднего балла):",
    ]
    for m in data.managers:
        lines.append(
            f"- {m.name} ({m.department or 'отдел не указан'}): {m.avg_percent}% "
            f"[{m.zone}], звонков {m.n_calls}",
        )
        for i, c in enumerate(m.calls[:5], 1):
            lines.append(
                f"    звонок {i}: {c.percent}% — {c.summary} "
                f"| сильные: {c.strengths} | зона роста: {c.growth_zone} "
                f"| обучение: {c.training_recommendation} "
                f"| флаги: {', '.join(c.red_flags) or 'нет'}",
            )
    lines.append("")
    lines.append("Самые слабые критерии команды (средний % от максимума):")
    for cr in data.weakest_criteria:
        lines.append(
            f"- #{cr.criterion_id} [{cr.block_name}] {cr.criterion_text}: "
            f"{cr.avg_pct_of_max}%",
        )
    return "\n".join(lines)


class ReportWriter:
    """Generate the narrative via an OpenAI Structured-Outputs model."""

    def __init__(self, model: str | None = None) -> None:
        self.model = model or settings.report_model
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            from openai import AsyncOpenAI  # noqa: PLC0415

            if not settings.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY is not set.")
            self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        return self._client

    async def write(self, data: ReportData) -> ReportNarrative:
        """Return the written report sections for the aggregated data."""
        if data.n_scored == 0:
            return ReportNarrative(
                overall_assessment="За период нет оценённых звонков.",
                manager_notes=[],
                systemic_problems=[],
                recommendations=[],
                conclusion="Данных для анализа нет.",
            )
        client = self._get_client()
        completion = await client.beta.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _data_block(data)},
            ],
            response_format=ReportNarrative,
            temperature=0.2,
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError("Report writer returned no parsed output")
        return parsed
