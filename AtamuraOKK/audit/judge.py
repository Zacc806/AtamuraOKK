"""LLM judge — check whether a call transcript supports a deal's stated close reason.

Claude via forced tool-use (same structured-output trick as
``scoring/anthropic_scorer.py``), returning ``supported`` / ``contradicted`` /
``not_determinable`` + confidence + a short justification and evidence quote.
Per-call errors (e.g. the API being out of credits) degrade to ``verdict="error"``
so a batch never aborts — the offline script and the standing audit pass share this.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

from AtamuraOKK.audit.notes import format_notes
from AtamuraOKK.settings import settings

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

VERDICTS = ("supported", "contradicted", "not_determinable")
_TOOL_NAME = "record_reason_verdict"
_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "justification": {
            "type": "string",
            "description": "Кратко на русском, 1-2 предложения.",
        },
        "evidence_quote": {
            "type": "string",
            "description": "Дословная цитата из транскрипта или пусто.",
        },
    },
    "required": ["verdict", "confidence", "justification", "evidence_quote"],
}
_SYSTEM = (
    "Ты — аудитор отдела контроля качества риелторского колл-центра. Менеджер закрыл "
    "лид с указанной причиной отказа. Тебе дают эту причину и транскрипт(ы) реальных "
    "звонков с клиентом (роли [AGENT]=менеджер, [CUSTOMER]=клиент). Определи, "
    "подтверждает ли разговор указанную причину закрытия.\n"
    "- supported — в разговоре есть прямое подтверждение указанной причины. "
    "Причины, которые клиент сам проговаривает про свою ситуацию (уже купил — "
    "у нас или в другом ЖК; ищет другой тип/локацию/готовое/коммерческое; "
    "далёкий горизонт покупки; не ЛПР; ошибочная заявка; продаёт своё жильё), "
    "считай supported при явном заявлении клиента — не уходи в not_determinable, "
    "если факт прозвучал в разговоре.\n"
    "- contradicted — разговор явно противоречит причине (клиент говорил другое).\n"
    "- not_determinable — по транскрипту действительно нельзя судить: факт лежит "
    "вне разговора и клиент его не озвучивает — например частота дозвонов "
    "(«недозвон», «не берёт трубку»), или решение банка по ипотеке, о котором "
    "клиент не упомянул.\n"
    "Кроме транскрипта тебе могут дать заметки менеджера из карточки сделки в CRM. "
    "Это его собственные пометки по клиенту, часто написанные уже после звонка, и "
    "причина закрытия нередко зафиксирована именно там, а в разговоре не звучит. "
    "Считай их таким же свидетельством: если заметка прямо подтверждает причину, это "
    "supported, даже когда в транскрипте подтверждения нет. Заметка не отменяет "
    "прямого противоречия в разговоре: если клиент сказал обратное тому, что написано "
    "в заметке, это contradicted. Заметки бывают в телеграфном стиле и с сокращениями "
    "(«ндз» — недозвон, «пв» — первоначальный взнос, «оп» — отдел продаж) — не "
    "додумывай за них того, чего там нет.\n"
    "Опирайся только на транскрипт и заметки, не додумывай."
)


def _blank_verdict() -> dict[str, Any]:
    return {
        "verdict": "error",
        "confidence": 0.0,
        "justification": "",
        "evidence_quote": "",
    }


def build_judge_client() -> AsyncAnthropic:
    """Construct the Anthropic client (raises if the API key is unset)."""
    from anthropic import AsyncAnthropic  # noqa: PLC0415

    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ATAMURAOKK_ANTHROPIC_API_KEY is not set — cannot run the audit judge.",
        )
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


def build_request(
    *,
    transcript: str,
    close_reason: str,
    notes: list[str] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """The ``messages.create`` parameters for judging one deal.

    Split out of :func:`judge_one` so the realtime path and the Batches path
    (``audit/batch.py``) send byte-identical requests — a verdict must not depend on
    which route produced it.

    ``notes`` are the manager's own timeline notes on the deal card
    (``audit/notes.py``): the stated reason is often written there and never said on the
    call, so they count as evidence next to the transcript. Empty → transcript alone.
    """
    notes_block = format_notes(notes or [])
    user = (
        f"Указанная причина закрытия: «{close_reason}»\n\n"
        f"Транскрипт(ы) звонков с клиентом:\n{transcript}"
    )
    if notes_block:
        user += f"\n\nЗаметки менеджера в карточке сделки (CRM):\n{notes_block}"
    return {
        "model": model or settings.anthropic_scoring_model,
        "max_tokens": settings.anthropic_max_tokens,
        "temperature": 0,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": user}],
        "tools": [
            {
                "name": _TOOL_NAME,
                "description": (
                    "Запиши вердикт: подтверждает ли звонок причину закрытия."
                ),
                "input_schema": _TOOL_SCHEMA,
            },
        ],
        "tool_choice": {"type": "tool", "name": _TOOL_NAME},
    }


def parse_verdict(content: list[Any]) -> dict[str, Any]:
    """The forced tool call out of a judge response, or a blank ``error`` verdict."""
    for block in content:
        if block.type == "tool_use" and block.name == _TOOL_NAME:
            return dict(block.input)
    return _blank_verdict()


async def judge_one(
    client: AsyncAnthropic,
    *,
    transcript: str,
    close_reason: str,
    notes: list[str] | None = None,
    model: str | None = None,
    sem: asyncio.Semaphore | None = None,
) -> dict[str, Any]:
    """Judge one deal realtime; returns a verdict dict (``verdict="error"`` on failure).

    The cheaper route for a backlog nobody is waiting on is ``audit/batch.py`` (half
    price, same request). This path exists for the interactive/offline CLI and for
    ``audit_judge_batch_enabled=False``.
    """
    verdict = _blank_verdict()
    try:
        async with _MaybeSemaphore(sem):
            resp = await client.messages.create(  # type: ignore[call-overload]
                **build_request(
                    transcript=transcript,
                    close_reason=close_reason,
                    notes=notes,
                    model=model,
                ),
            )
        verdict = parse_verdict(resp.content)
    except Exception as exc:  # record, don't abort the batch
        verdict["justification"] = f"{type(exc).__name__}: {exc}"
        logger.warning("audit judge failed: {e}", e=exc)
    return verdict


class _MaybeSemaphore:
    """Async-with over an optional semaphore (no-op when None)."""

    def __init__(self, sem: asyncio.Semaphore | None) -> None:
        self._sem = sem

    async def __aenter__(self) -> None:
        if self._sem is not None:
            await self._sem.acquire()

    async def __aexit__(self, *exc: object) -> None:
        if self._sem is not None:
            self._sem.release()
