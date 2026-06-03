"""YandexGPT scorer (Kazakh / "шала казахский" calls).

Yandex handles Kazakh and Kazakh-Russian mixed speech better than the Russian
path, so Kazakh-routed calls go here. Same prompt + retry loop as Groq; only
the HTTP transport differs (YandexGPT foundationModels completion API).
"""

from __future__ import annotations

from typing import Any

import httpx

from AtamuraOKK.scoring.errors import ProviderUnavailableError, ScoringError
from AtamuraOKK.scoring.llm import BaseLLMScorer
from AtamuraOKK.scoring.rubric import Rubric

_COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
_HTTP_TOO_MANY = 429
_HTTP_SERVER_ERROR = 500


class YandexScorer(BaseLLMScorer):
    """Score a call with YandexGPT returning JSON."""

    provider = "yandex"

    def __init__(
        self,
        rubric: Rubric,
        *,
        api_key: str = "",
        folder_id: str = "",
        model: str = "yandexgpt/latest",
        client: httpx.AsyncClient | None = None,
        max_retries: int = 5,
        retry_base_delay: float = 1.0,
        max_transcript_chars: int = 24000,
        pass_threshold: int = 75,
    ) -> None:
        super().__init__(
            rubric,
            model=model,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
            max_transcript_chars=max_transcript_chars,
            pass_threshold=pass_threshold,
        )
        self._api_key = api_key
        self._folder_id = folder_id
        self._client = client

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=90.0)
        return self._client

    async def _raw_complete(self, prompt: str) -> str:
        client = self._ensure_client()
        payload = {
            "modelUri": f"gpt://{self._folder_id}/{self.model}",
            "completionOptions": {
                "stream": False,
                "temperature": 0.2,
                "maxTokens": "1200",
            },
            "messages": [{"role": "user", "text": prompt}],
        }
        headers = {"Authorization": f"Api-Key {self._api_key}"}
        try:
            resp = await client.post(_COMPLETION_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"yandex: {exc}") from exc

        if resp.status_code == _HTTP_TOO_MANY or resp.status_code >= _HTTP_SERVER_ERROR:
            raise ProviderUnavailableError(f"yandex: HTTP {resp.status_code}")
        if resp.status_code != httpx.codes.OK:
            raise ScoringError(f"yandex: HTTP {resp.status_code} {resp.text[:200]}")

        return _extract_text(resp.json())


def _extract_text(data: dict[str, Any]) -> str:
    try:
        return str(data["result"]["alternatives"][0]["message"]["text"])
    except (KeyError, IndexError, TypeError) as exc:
        raise ScoringError(f"yandex: unexpected response shape: {exc}") from exc
