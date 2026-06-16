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

Сигналы для отдела продаж (заполняй по содержанию разговора):
- payment_method — способ оплаты, который клиент назвал или подтвердил: \
«наличные», «ипотека», «рассрочка», «другое»; «неизвестно», если тема оплаты не \
поднималась. Ставь «наличные» только при явном указании на покупку за наличные / \
без ипотеки и рассрочки.
- wants_to_visit — true, если клиент согласился приехать в офис или на показ \
квартир (КЭВ) либо явно выразил такое намерение; иначе false.
- on_premises — true, только если из разговора следует, что клиент уже находится \
в офисе или на объекте; иначе false.
"""


def present_transcript(transcript: str) -> str:
    """Relabel channel headers neutrally so role isn't implied by the label."""
    for old, new in _RELABEL.items():
        transcript = transcript.replace(old, new)
    return transcript


# Human label for the client's lead category in the prompt.
_CATEGORY_NAME = {
    "A": "A (горячий)",
    "B": "B (тёплый)",
    "C": "C (холодный)",
    "X": "X (неуспешный разговор)",
}

# Per-category guidance for the meeting-closing criterion «Закрытие на КЭВ».
# A / None / X keep the default (hard meeting push). Only B and C differ.
_CATEGORY_NOTE = {
    "B": (
        "Клиент категории B (тёплый) по регламенту квалификации: немедленная встреча "
        "НЕ ожидается — менеджер продолжает работать с лидом. По критерию «Закрытие на "
        "КЭВ» засчитывай как успех договорённость о следующем контакте / фоллоу-апе "
        "(например, через 1–2 недели), а не жёсткий дожим на встречу прямо сейчас. "
        "Оценивай этот критерий по сокращённой шкале (см. max в чек-листе)."
    ),
    "C": (
        "Клиент категории C (холодный) по регламенту квалификации: встреча запрещена и "
        "в план не засчитывается. Критерий «Закрытие на КЭВ» НЕ оценивается и исключён "
        "из чек-листа — не выставляй по нему балл и не включай его в массив criteria."
    ),
}


def _checklist(rubric: Rubric, category: str | None) -> str:
    """Render the checklist, using per-category maxima.

    A criterion whose category-max is 0 (e.g. «Закрытие на КЭВ» for category C) is
    omitted entirely — the model is told not to score it and ``_assemble`` excludes
    it too. Reduced maxima (category B) are shown so the model scores on that scale.
    """
    lines: list[str] = []
    current_block = ""
    for c in rubric.scored_criteria:
        eff_max = rubric.max_for(c, category)
        if eff_max is None:
            continue
        if c.block_name != current_block:
            current_block = c.block_name
            lines.append(f"\n## {c.block_name}")
        lines.append(f"{c.id}. (max {eff_max}) {c.text}")
    return "\n".join(lines)


def build_messages(
    transcript: str,
    rubric: Rubric,
    direction: str,
    client_category: str | None = None,
) -> list[dict[str, str]]:
    """Return chat messages for the scorer."""
    checklist = _checklist(rubric, client_category)

    direction_ru = {
        "outbound": "исходящий (компания звонит клиенту)",
        "inbound": "входящий (звонят в компанию)",
    }.get(direction, "направление неизвестно")

    cat_label = _CATEGORY_NAME.get(client_category or "", "не указана")
    cat_lines = f"Категория клиента: {cat_label}.\n"
    note = _CATEGORY_NOTE.get(client_category or "")
    if note:
        cat_lines += f"{note}\n"

    user = (
        f"Направление звонка: {direction_ru}.\n"
        f"{cat_lines}\n"
        f"ЧЕК-ЛИСТ (оцени каждый критерий по его id, 0..max):\n{checklist}\n\n"
        f"ТРАНСКРИПТ ЗВОНКА:\n{present_transcript(transcript)}\n\n"
        "Верни строго по схеме: call_type, is_qualification_call, "
        "manager_identified, массив criteria "
        "{id, score, justification, evidence, recommendation} "
        "для КАЖДОГО критерия выше, objections_present, тональности, резюме, "
        "красные флаги, целевой статус, сильные стороны, зону роста, "
        "рекомендацию по обучению, payment_method, wants_to_visit, on_premises."
    )
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]
