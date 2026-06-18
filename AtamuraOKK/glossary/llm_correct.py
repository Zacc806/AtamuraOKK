"""LLM post-transcription correction of ЖК names & addresses.

Yandex SpeechKit v3 cannot be given a vocabulary up front, so it mishears the
residential-complex names and the Kazakh toponyms in the addresses. After
transcription we hand the raw text plus the canonical glossary
(:mod:`AtamuraOKK.glossary.canonical`) to a cheap Claude model that repairs
*only* those named entities and returns the transcript otherwise verbatim.

The corrector is deliberately decoupled from both pipelines' settings: the caller
passes ``api_key`` and ``model`` in, mirroring the lazy-client pattern in
``scoring/anthropic_scorer.py`` and ``scoring/meetings/anthropic.py``. It never
raises into the worker — on any failure it returns the original text unchanged,
so a correction outage can never block or fail a transcription.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from AtamuraOKK.glossary.canonical import build_reference_text

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

_SYSTEM_PROMPT = (
    "Ты — редактор расшифровок телефонных разговоров агентства недвижимости. "
    "Твоя единственная задача — исправить названия жилых комплексов (ЖК) и "
    "адреса, опираясь на справочник ниже. Speech-to-текст часто искажает их, "
    "особенно казахские топонимы.\n\n"
    "СТРОГИЕ ПРАВИЛА:\n"
    "1. Исправляй ТОЛЬКО названия ЖК и адреса (улицы, микрорайоны, сёла, "
    "районы, топонимы) на канонические написания из справочника.\n"
    "2. Не перефразируй, не переводи, не сокращай, не дополняй и не меняй "
    "порядок — весь остальной текст должен остаться слово в слово как есть.\n"
    "3. Сохраняй метки говорящих ([AGENT], [CUSTOMER] и подобные), пунктуацию, "
    "переносы строк и структуру в точности.\n"
    "4. Если в реплике явно упоминается один из ЖК или его адрес, но искажённо — "
    "приведи к каноническому виду. Если совпадения нет — не трогай текст.\n"
    "5. Верни ТОЛЬКО исправленную расшифровку, без пояснений и комментариев.\n\n"
    "СПРАВОЧНИК:\n" + build_reference_text()
)

_HTTP_SERVER_ERROR = 500


class EntityCorrector:
    """Repair ЖК names & addresses in a transcript via a cheap Claude model."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_tokens: int = 1500,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._max_tokens = max_tokens
        self._client = client

    def _ensure_client(self) -> AsyncAnthropic:
        if self._client is None:
            from anthropic import AsyncAnthropic  # noqa: PLC0415

            self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def correct(self, text: str) -> str:
        """Return ``text`` with ЖК names & addresses canonicalised.

        Returns the original ``text`` unchanged on empty input or on any model
        failure (API error, empty response, truncation) — correction is best
        effort and must never break the pipeline.
        """
        if not text.strip():
            return text

        from anthropic import APIError  # noqa: PLC0415

        client = self._ensure_client()
        try:
            resp = await client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                temperature=0,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
            )
        except APIError as exc:
            logger.warning("Glossary correction failed ({e}); keeping raw text", e=exc)
            return text
        except Exception as exc:  # never let correction break transcription
            logger.warning(
                "Glossary correction crashed ({e}); keeping raw text", e=exc
            )
            return text

        if resp.stop_reason == "max_tokens":
            logger.warning(
                "Glossary correction truncated (max_tokens); keeping raw text"
            )
            return text

        corrected = "".join(
            getattr(block, "text", "")
            for block in resp.content
            if getattr(block, "type", None) == "text"
        ).strip()
        if not corrected:
            logger.warning("Glossary correction returned empty; keeping raw text")
            return text
        return corrected


async def correct_entities(text: str, *, api_key: str, model: str) -> str:
    """One-shot convenience wrapper around :class:`EntityCorrector`."""
    return await EntityCorrector(api_key=api_key, model=model).correct(text)
