"""Manipulation detector (ТЗ 2.1, 🔴): manager claims vs ЖК ground truth.

A focused Claude pass that, given the ЖК fact sheets and a transcript, extracts
the manager's factual claims (лифт, этажность, отделка, сроки, банки, скидки) and
flags any that contradict reality. Independent of the rubric score — its output
is a list of red flags + an admin alert, not a point deduction.

Inert without data: if no ЖК facts are loaded the detector returns ``[]`` (the
business populates ``scoring/zhk/`` per the ТЗ).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from AtamuraOKK.scoring.errors import ProviderUnavailableError, ScoringError
from AtamuraOKK.scoring.zhk import ZhkFacts

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

_HTTP_TOO_MANY = 429
_HTTP_SERVER_ERROR = 500
_MAX_OUTPUT_TOKENS = 1000
_FENCE_RE = re.compile(r"^```(?:json)?\n?|\n?```$")
_ARR_RE = re.compile(r"\[.*\]", re.DOTALL)


@dataclass(slots=True)
class Manipulation:
    """One detected contradiction between a manager claim and ЖК reality."""

    zhk: str
    claim: str  # what the manager said
    reality: str  # what the KB says
    severity: str  # "low" | "medium" | "high"

    def to_dict(self) -> dict[str, str]:
        """JSON-serializable form."""
        return asdict(self)


def _build_prompt(facts: list[ZhkFacts], text: str, *, max_chars: int) -> str:
    sheets = "\n".join(f"- {f.render()}" for f in facts)
    return "\n".join(
        [
            "Ты аудитор отдела контроля качества Атамура Групп.",
            "Ниже — ПРОВЕРЕННЫЕ факты по ЖК и транскрипт встречи менеджера.",
            "Найди УТВЕРЖДЕНИЯ менеджера, которые ПРОТИВОРЕЧАТ фактам (обман клиента):",
            "лифт, этажность, отделка, сроки сдачи, банки, скидки, площади.",
            "Не придумывай: если факта нет в списке — не суди о нём.",
            "",
            "ФАКТЫ ПО ЖК:",
            sheets,
            "",
            "ВЕРНИ строго JSON-массив (без markdown), пустой [] если нарушений нет:",
            '[{"zhk": "Аура", "claim": "сказал что есть лифт",'
            ' "reality": "в Ауре лифта нет", "severity": "high"}]',
            "severity: high (вводит в заблуждение по ключевому факту) | medium | low.",
            "",
            "ТРАНСКРИПТ:",
            text[:max_chars],
        ],
    )


class ManipulationDetector:
    """Compare manager claims against the ЖК KB via one Claude call."""

    def __init__(
        self,
        facts: list[ZhkFacts],
        *,
        api_key: str = "",
        model: str = "claude-sonnet-4-6",
        client: AsyncAnthropic | None = None,
        max_transcript_chars: int = 24000,
    ) -> None:
        self.facts = facts
        self.api_key = api_key
        self.model = model
        self._client = client
        self.max_transcript_chars = max_transcript_chars

    def _ensure_client(self) -> AsyncAnthropic:
        if self._client is None:
            from anthropic import AsyncAnthropic  # noqa: PLC0415

            self._client = AsyncAnthropic(api_key=self.api_key)
        return self._client

    async def _raw_complete(self, prompt: str) -> str:
        from anthropic import (  # noqa: PLC0415
            APIConnectionError,
            APIStatusError,
            RateLimitError,
        )

        client = self._ensure_client()
        try:
            resp = await client.messages.create(
                model=self.model,
                max_tokens=_MAX_OUTPUT_TOKENS,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
        except (RateLimitError, APIConnectionError) as exc:
            raise ProviderUnavailableError(f"anthropic: {exc}") from exc
        except APIStatusError as exc:
            transient = (
                exc.status_code == _HTTP_TOO_MANY
                or exc.status_code >= _HTTP_SERVER_ERROR
            )
            if transient:
                raise ProviderUnavailableError(f"anthropic: {exc}") from exc
            raise ScoringError(f"anthropic: {exc}") from exc
        return "".join(
            getattr(b, "text", "") for b in resp.content
            if getattr(b, "type", None) == "text"
        ).strip()

    async def detect(self, transcript_text: str) -> list[Manipulation]:
        """Return manipulations found in the transcript (``[]`` if KB empty)."""
        if not self.facts or not transcript_text.strip():
            return []
        prompt = _build_prompt(
            self.facts,
            transcript_text,
            max_chars=self.max_transcript_chars,
        )
        raw = await self._raw_complete(prompt)
        return _parse_manipulations(raw)


def _parse_manipulations(answer: str) -> list[Manipulation]:
    """Parse the detector's JSON array; tolerant of fences/prose, never raises."""
    cleaned = _FENCE_RE.sub("", answer.strip())
    match = _ARR_RE.search(cleaned)
    if not match:
        return []
    try:
        rows = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    out: list[Manipulation] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            Manipulation(
                zhk=str(row.get("zhk", "")),
                claim=str(row.get("claim", "")),
                reality=str(row.get("reality", "")),
                severity=str(row.get("severity", "medium")),
            ),
        )
    return out
