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
    "- supported — в разговоре есть прямое подтверждение указанной причины.\n"
    "- contradicted — разговор явно противоречит причине (клиент говорил другое).\n"
    "- not_determinable — по транскрипту нельзя судить. Причины про частоту звонков "
    "(«недозвон», «не берёт трубку») почти всегда not_determinable, так как "
    "отвеченный звонок этого не показывает.\n"
    "Опирайся только на транскрипт, не додумывай."
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


async def judge_one(
    client: AsyncAnthropic,
    *,
    transcript: str,
    close_reason: str,
    model: str | None = None,
    sem: asyncio.Semaphore | None = None,
) -> dict[str, Any]:
    """Judge one deal; returns a verdict dict (``verdict="error"`` on failure)."""
    model = model or settings.anthropic_scoring_model
    user = (
        f"Указанная причина закрытия: «{close_reason}»\n\n"
        f"Транскрипт(ы) звонков с клиентом:\n{transcript}"
    )
    tool = {
        "name": _TOOL_NAME,
        "description": "Запиши вердикт: подтверждает ли звонок причину закрытия.",
        "input_schema": _TOOL_SCHEMA,
    }
    verdict = _blank_verdict()
    try:
        async with _MaybeSemaphore(sem):
            resp = await client.messages.create(  # type: ignore[call-overload]
                model=model,
                max_tokens=settings.anthropic_max_tokens,
                temperature=0,
                system=_SYSTEM,
                messages=[{"role": "user", "content": user}],
                tools=[tool],
                tool_choice={"type": "tool", "name": _TOOL_NAME},
            )
        for block in resp.content:
            if block.type == "tool_use" and block.name == _TOOL_NAME:
                verdict = dict(block.input)
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
