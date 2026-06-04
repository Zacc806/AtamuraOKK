"""Bilingual (RU/KK) scoring prompt, built from the rubric.

Ported from the proven ``compliance_checker.build_prompt`` and parameterized by
:class:`~AtamuraOKK.scoring.rubric.Rubric` so adding/retiring a criterion is a
rubric edit, never a prompt edit. The same text is sent to both providers.
"""

from __future__ import annotations

import json

from AtamuraOKK.scoring.rubric import Rubric
from AtamuraOKK.scoring.script import Script


def _objection_ids(rubric: Rubric) -> list[int]:
    if rubric.objection_block is None:
        return []
    return rubric.blocks.get(rubric.objection_block, [])


def build_prompt(
    rubric: Rubric,
    *,
    text: str,
    duration_sec: int,
    max_chars: int,
    script: Script | None = None,
) -> str:
    """Build the scoring prompt for one call.

    :param rubric: the active rubric.
    :param text: speaker-tagged transcript text.
    :param duration_sec: call duration (for the model's context).
    :param max_chars: truncate the transcript to this many characters (cost guard).
    :param script: optional sales script to also measure adherence against.
    :returns: the full prompt string.
    """
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
        f"Оцени звонок менеджера по чек-листу из {len(ai)} критериев.",
        "По каждому критерию верни балл от 0 до max_score.",
        "",
        "РОЛИ В ДИАЛОГЕ:",
        "- [agent] = менеджер (его и оцениваем), [customer] = клиент.",
        "- Если реплики не размечены ([unknown] или без тегов) — определи роли сам по",
        "  содержанию: менеджер здоровается, презентует ЖК, отрабатывает возражения,",
        "  дожимает на встречу; клиент спрашивает, сомневается, соглашается.",
        "- Оцениваешь работу МЕНЕДЖЕРА, не клиента.",
        "",
        "ТИП ЗВОНКА (определи и учти контекст):",
        "- первичный (новый лид) — оцениваешь по полному чек-листу;",
        "- повторный (клиент уже знаком) — НЕ штрафуй за отсутствие приветствия и",
        "  программирования: ставь по этим пунктам полный балл;",
        "- уточняющий (короткий вопрос) — оцени релевантность ответа, не требуй КЭВ;",
        "- сервисный (после визита) — оцени эмпатию и помощь, не закрытие на встречу.",
        "Верни тип в поле call_type.",
        "",
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
        '  "red_flags_found": [],',
        *script_json,
        '  "summary": "Менеджер хорошо отработал, клиент записан"',
        "}",
        "",
        "ПРАВИЛА:",
        "- scores: ключи — id критериев (строкой), значения от 0 до max_score",
        "- client_agreed_meeting = true только если клиент явно согласился",
        "  на встречу / приехать в офис",
        '- manager_tone = "вежливый" / "нейтральный" / "грубый" / "неуверенный"',
        "- Звонок может быть на русском или казахском — оценивай одинаково строго",
        "- Казахские приветствия («Сәлеметсіз бе», «Ассалаумағалейкум»,",
        "  «Қайырлы күн/кеш») и микс с русским — это валидное приветствие",
        "- Small talk про погоду/жару — норма в РК, не считай тратой времени",
        "- Транскрипция местами с ошибками распознавания — игнорируй опечатки",
        *objection_rule,
        *script_rules,
        "",
        f"Длительность звонка: {duration_sec} секунд",
        "",
        "Транскрипция:",
        text[:max_chars],
    ]
    return "\n".join(parts)
