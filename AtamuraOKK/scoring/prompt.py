"""Build the bilingual (RU/KK) scoring prompt from the rubric + transcript."""

from __future__ import annotations

from AtamuraOKK.scoring.rubric import Rubric

# Channel labels are by AUDIO CHANNEL, not by role — the manager is sometimes on
# either channel. Relabel neutrally so the model identifies the manager by content.
_RELABEL = {
    "[AGENT]": "СТОРОНА A (аудиоканал 1)",
    "[CUSTOMER]": "СТОРОНА B (аудиоканал 2)",
    "[UNKNOWN]": "ЗАПИСЬ (один канал, говорящие не разделены)",
}

_SYSTEM = """\
Ты — аудитор отдела контроля качества (ОКК) Atamura Group (продажа недвижимости, \
отдел телемаркетинга/ТМ). Оцени работу МЕНЕДЖЕРА ТМ по записи звонка по чек-листу.

Сначала классифицируй звонок (call_type) и реши, применим ли чек-лист
(is_qualification_call):
- «квалификация» — настоящий звонок ТМ клиенту по заявке: менеджер квалифицирует \
клиента и ведёт на встречу. Чек-лист применим (is_qualification_call=true).
- «напоминание» — менеджер лишь напоминает о уже назначенной встрече.
- «вендор_или_спам» — звонящий продаёт что-то НАМ / реклама.
- «внутренний» — сотрудники общаются между собой (например, проверка связи).
- «недозвон_или_ошибка» — разговора по сути нет / не туда попали.
- «повторный_сервисный» / «другое».
Для всех типов, КРОМЕ «квалификация», ставь is_qualification_call=false — такие \
звонки не входят в оценку качества по чек-листу (баллы будут низкими, это нормально).

Определение менеджера:
- Метки СТОРОНА A / СТОРОНА B — это просто аудиоканалы, НЕ роли. Менеджер Atamura \
тот, кто представляет компанию, задаёт квалифицирующие вопросы, презентует ЖК и \
ведёт к встрече — он может быть на любой стороне. Определи его сам и оценивай \
ТОЛЬКО его реплики (manager_identified=true). Если менеджера определить нельзя — \
manager_identified=false и is_qualification_call=false.

Оценка:
- Звонок может быть на русском или казахском; оценивай одинаково строго, цитаты \
приводи на языке оригинала.
- Каждый из 5 критериев оценивай ЦЕЛОСТНО (холистически) по всей его шкале \
0..max, учитывая все аспекты из описания критерия, а НЕ по принципу «всё или \
ничего». Ориентиры:
  • max — выполнено полностью и качественно по всем аспектам критерия;
  • ~75% от max — выполнено, но с заметными недочётами / часть аспектов слабее;
  • ~50% от max — выполнено частично либо предпринята явная, но неполная попытка;
  • ~25% от max — слабая, формальная или вскользь затронутая попытка;
  • 0 — менеджер не сделал по критерию НИЧЕГО / критерий отсутствует в разговоре.
- Округляй до целого балла. Ставь 0 только при полном отсутствии действия — любую \
частичную работу засчитывай частичным баллом, а не нулём.
- Если у клиента НЕ было возражений — поставь objections_present=false. В этом \
случае критерий «Отработка возражений» НЕ оценивается и не влияет на итоговый балл \
(балл по нему можешь не заполнять).
- Для каждого критерия дай justification (краткое обоснование, рус.), evidence \
(цитата из разговора на языке оригинала или пусто) и recommendation (конкретная \
рекомендация менеджеру: что улучшить по этому критерию на следующем звонке, рус.).
- Дополнительно: тональности, резюме, красные флаги, целевой статус, сильные \
стороны, зона роста, рекомендация по обучению — на русском.
"""


def present_transcript(transcript: str) -> str:
    """Relabel channel headers neutrally so role isn't implied by the label."""
    for old, new in _RELABEL.items():
        transcript = transcript.replace(old, new)
    return transcript


def build_messages(
    transcript: str,
    rubric: Rubric,
    direction: str,
) -> list[dict[str, str]]:
    """Return chat messages for the scorer."""
    lines: list[str] = []
    current_block = ""
    for c in rubric.scored_criteria:
        if c.block_name != current_block:
            current_block = c.block_name
            lines.append(f"\n## {c.block_name}")
        lines.append(f"{c.id}. (max {c.max}) {c.text}")
    checklist = "\n".join(lines)

    direction_ru = {
        "outbound": "исходящий (компания звонит клиенту)",
        "inbound": "входящий (звонят в компанию)",
    }.get(direction, "направление неизвестно")

    user = (
        f"Направление звонка: {direction_ru}.\n\n"
        f"ЧЕК-ЛИСТ (оцени каждый критерий по его id, 0..max):\n{checklist}\n\n"
        f"ТРАНСКРИПТ ЗВОНКА:\n{present_transcript(transcript)}\n\n"
        "Верни строго по схеме: call_type, is_qualification_call, "
        "manager_identified, массив criteria "
        "{id, score, justification, evidence, recommendation} "
        "для КАЖДОГО критерия выше, objections_present, тональности, резюме, "
        "красные флаги, целевой статус, сильные стороны, зону роста, "
        "рекомендацию по обучению."
    )
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]
