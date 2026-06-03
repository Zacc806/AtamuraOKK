"""Bilingual (RU/KK) scoring prompt, built from the rubric.

Ported from the proven ``compliance_checker.build_prompt`` and parameterized by
:class:`~AtamuraOKK.scoring.rubric.Rubric` so adding/retiring a criterion is a
rubric edit, never a prompt edit. The same text is sent to both providers.
"""

from __future__ import annotations

import json

from AtamuraOKK.scoring.rubric import Rubric


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
) -> str:
    """Build the scoring prompt for one call.

    :param rubric: the active rubric.
    :param text: speaker-tagged transcript text.
    :param duration_sec: call duration (for the model's context).
    :param max_chars: truncate the transcript to this many characters (cost guard).
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

    parts = [
        "Ты эксперт отдела контроля качества компании Атамура Групп.",
        "",
        f"Оцени звонок менеджера по чек-листу из {len(ai)} критериев.",
        "По каждому критерию верни балл от 0 до max_score.",
        "",
        "ЧЕК-ЛИСТ:",
        criteria_desc,
        "",
        "КРАСНЫЕ ФЛАГИ (если заметишь):",
        red_flags,
        "",
        "ОТВЕТЬ строго в JSON формате (без markdown, без объяснений):",
        "{",
        f'  "scores": {example_scores},',
        '  "client_agreed_meeting": true,',
        '  "manager_tone": "вежливый",',
        '  "red_flags_found": [],',
        '  "summary": "Менеджер хорошо отработал, клиент записан"',
        "}",
        "",
        "ПРАВИЛА:",
        "- scores: ключи — id критериев (строкой), значения от 0 до max_score",
        "- client_agreed_meeting = true только если клиент явно согласился",
        "  на встречу / приехать в офис",
        '- manager_tone = "вежливый" / "нейтральный" / "грубый" / "неуверенный"',
        "- Звонок может быть на русском или казахском — оценивай одинаково строго",
        "- Транскрипция местами с ошибками распознавания — игнорируй опечатки",
        *objection_rule,
        "",
        f"Длительность звонка: {duration_sec} секунд",
        "",
        "Транскрипция:",
        text[:max_chars],
    ]
    return "\n".join(parts)
