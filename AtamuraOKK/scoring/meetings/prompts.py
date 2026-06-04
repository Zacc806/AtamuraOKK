"""Bilingual (RU/KK) scoring prompt, built from the rubric.

Ported from the proven ``compliance_checker.build_prompt`` and parameterized by
:class:`~AtamuraOKK.scoring.meetings.rubric.Rubric` so adding/retiring a criterion is a
rubric edit, never a prompt edit. The same text is sent to both providers.
"""

from __future__ import annotations

import json

from AtamuraOKK.scoring.meetings.rubric import Rubric
from AtamuraOKK.scoring.meetings.script import Script

_MEETING_CONTEXT = "op_meeting"


def _objection_ids(rubric: Rubric) -> list[int]:
    if rubric.objection_block is None:
        return []
    return rubric.blocks.get(rubric.objection_block, [])


def _roles_section(*, is_meeting: bool) -> list[str]:
    base = [
        "РОЛИ В ДИАЛОГЕ:",
        "- [agent] = менеджер (его и оцениваем), [customer] = клиент.",
    ]
    if is_meeting:
        base += [
            "- [third_party] = третьи лица (родственники, друзья) — НЕ оцениваются.",
            "- Если спикеры размечены диаризацией ([speaker_1]/[speaker_2]/...) —",
            "  определи, кто из них менеджер (ведёт, презентует ЖК, дожимает), кто",
            "  клиент, кто третьи лица.",
            "- Если реплики не размечены — определи роли сам: менеджер здоровается,",
            "  презентует ЖК, отрабатывает возражения, ведёт к брони; клиент",
            "  спрашивает, сомневается, соглашается.",
        ]
    else:
        base += [
            "- Если реплики не размечены ([unknown] или без тегов) — определи роли сам",
            "  по содержанию: менеджер здоровается, презентует ЖК, отрабатывает",
            "  возражения, дожимает на встречу; клиент спрашивает, сомневается.",
        ]
    base.append("- Оцениваешь работу МЕНЕДЖЕРА, не клиента.")
    return base


def _type_section(*, is_meeting: bool) -> list[str]:
    if is_meeting:
        return [
            "ТИП ВСТРЕЧИ (определи и учти контекст):",
            "- первичная (клиент впервые) — оцениваешь по полному чек-листу;",
            "- повторная (клиент уже был / знаком) — НЕ штрафуй за отсутствие",
            "  приветствия и установления контакта: ставь по ним полный балл;",
            "- уточняющая (короткий визит/вопрос) — оцени релевантность, не требуй",
            "  закрытия на бронь;",
            "- сервисная (после сделки) — оцени эмпатию и помощь, не закрытие.",
            "Верни тип в поле call_type.",
            "",
            "Это может быть ФРАГМЕНТ длинной встречи — оценивай по тому, что есть в",
            "этом фрагменте; если этап (приветствие/закрытие) не виден здесь — ставь",
            "0, он мог быть в другой части записи (итоговый балл собирается по всем",
            "фрагментам, беря лучшее по каждому критерию).",
        ]
    return [
        "ТИП ЗВОНКА (определи и учти контекст):",
        "- первичный (новый лид) — оцениваешь по полному чек-листу;",
        "- повторный (клиент уже знаком) — НЕ штрафуй за отсутствие приветствия и",
        "  программирования: ставь по этим пунктам полный балл;",
        "- уточняющий (короткий вопрос) — оцени релевантность ответа, не требуй КЭВ;",
        "- сервисный (после визита) — оцени эмпатию и помощь, не закрытие на встречу.",
        "Верни тип в поле call_type.",
    ]


def _visit_section(visit_index: int, *, is_meeting: bool) -> list[str]:
    if visit_index <= 1:
        return []
    unit = "встреча" if is_meeting else "звонок"
    return [
        f"КОНТЕКСТ КЛИЕНТА (CRM): это {visit_index}-й {unit} с этим клиентом"
        " (повторный) — НЕ штрафуй за пропуск приветствия / установления контакта"
        " / программирования; клиент уже знаком.",
        "",
    ]


def build_prompt(
    rubric: Rubric,
    *,
    text: str,
    duration_sec: int,
    max_chars: int,
    script: Script | None = None,
    visit_index: int = 1,
) -> str:
    """Build the scoring prompt for one call or ОП meeting.

    The framing (call vs meeting) is driven by ``rubric.context``: an
    ``op_meeting`` rubric (okk_meeting_v1) yields meeting wording, the
    fragment-aware note, and a meeting duration label; otherwise the call
    framing is used. Criteria themselves always come from the rubric.

    :param rubric: the active rubric.
    :param text: speaker-tagged transcript text.
    :param duration_sec: call/meeting duration (for the model's context).
    :param max_chars: truncate the transcript to this many characters (cost guard).
    :param script: optional sales script to also measure adherence against.
    :param visit_index: client contact position (ТЗ 2.4); >1 adds repeat context.
    :returns: the full prompt string.
    """
    is_meeting = rubric.context == _MEETING_CONTEXT
    unit = "встречу менеджера ОП" if is_meeting else "звонок менеджера"
    duration_label = "Длительность встречи" if is_meeting else "Длительность звонка"
    agreed_rule = (
        "- client_agreed_meeting = true только если клиент оставил финансовую"
        " бронь (внёс предоплату / забронировал квартиру) — целевое действие"
        " встречи; устного «подумаю» / «перезвоню» НЕдостаточно"
        if is_meeting
        else "- client_agreed_meeting = true только если клиент явно согласился"
        " на встречу / приехать в офис"
    )

    ai = rubric.ai_criteria
    criteria_desc = "\n".join(
        f"{c.id}. [{c.block}] {c.name} (макс {c.max_score}): {c.check}" for c in ai
    )
    red_flags = "\n".join(f"- {f}" for f in rubric.red_flags)
    example_scores = json.dumps(
        {str(c.id): c.max_score for c in ai},
        ensure_ascii=False,
    )
    objection_ids = ", ".join(str(i) for i in _objection_ids(rubric))
    objection_rule = (
        [f"- Если возражений не было — ставь полный балл за пункты {objection_ids}"]
        if objection_ids
        else []
    )

    script_section = (
        [
            "",
            "СКРИПТ ПРОДАЖ (эталон — оцени, насколько менеджер ему следовал):",
            script.render(),
        ]
        if script is not None
        else []
    )
    script_json = (
        [
            '  "script_adherence": 85,',
            '  "script_deviations": ["где и как менеджер отклонился от скрипта"],',
        ]
        if script is not None
        else []
    )
    script_rules = (
        [
            "- script_adherence = 0-100: насколько менеджер следовал скрипту выше",
            "- script_deviations = список конкретных отклонений (пустой, если их нет)",
        ]
        if script is not None
        else []
    )

    parts = [
        "Ты эксперт отдела контроля качества компании Атамура Групп.",
        "",
        f"Оцени {unit} по чек-листу из {len(ai)} критериев.",
        "По каждому критерию верни балл от 0 до max_score.",
        "",
        *_roles_section(is_meeting=is_meeting),
        "",
        *_type_section(is_meeting=is_meeting),
        "",
        *_visit_section(visit_index, is_meeting=is_meeting),
        "ЧЕК-ЛИСТ:",
        criteria_desc,
        "",
        "КРАСНЫЕ ФЛАГИ (если заметишь):",
        red_flags,
        *script_section,
        "",
        "ОТВЕТЬ строго в JSON формате (без markdown, без объяснений):",
        "{",
        f'  "scores": {example_scores},',
        '  "call_type": "первичный",',
        '  "client_agreed_meeting": true,',
        '  "manager_tone": "вежливый",',
        '  "client_emotion": "спокоен",',
        '  "red_flags_found": [],',
        *script_json,
        '  "summary": "Менеджер хорошо отработал, клиент записан"',
        "}",
        "",
        "ПРАВИЛА:",
        "- scores: ключи — id критериев (строкой), значения от 0 до max_score",
        agreed_rule,
        '- manager_tone = "вежливый" / "нейтральный" / "грубый" / "неуверенный"',
        "- client_emotion = состояние клиента: спокоен / спешит / раздражён /"
        " эмоционален",
        "- НЕ штрафуй менеджера за адаптацию темпа/тона под состояние клиента"
        " (эмпатия к спешащему/эмоциональному клиенту — это плюс, не минус)",
        "- Разговор может быть на русском или казахском — оценивай одинаково строго",
        "- Казахские приветствия («Сәлеметсіз бе», «Ассалаумағалейкум»,",
        "  «Қайырлы күн/кеш») и микс с русским — это валидное приветствие",
        "- Small talk про погоду/жару — норма в РК, не считай тратой времени",
        "- Транскрипция местами с ошибками распознавания — игнорируй опечатки",
        "- Если в транскрипте отмечена невербалика ([пауза Nс], [неуверенно] и"
        " т.п.) — учитывай: паузы менеджера >5с и неуверенный тон снижают софт-"
        "скилы; если разметки нет — не выдумывай",
        *objection_rule,
        *script_rules,
        "",
        f"{duration_label}: {duration_sec} секунд",
        "",
        "Транскрипция:",
        text[:max_chars],
    ]
    return "\n".join(parts)
