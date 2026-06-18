"""LLM entity corrector — prompt content and never-block-the-pipeline fallback.

The Anthropic client is faked (injected via the ``client`` arg), so these tests
make no network calls.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from AtamuraOKK.glossary.llm_correct import EntityCorrector

pytestmark = pytest.mark.anyio

_RAW = "[CUSTOMER] Интересует жк атмасфера на улице сырым датулин."


def _block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _response(text: str, *, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop_reason, content=[_block(text)])


class _FakeMessages:
    def __init__(self, *, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._response


class _FakeClient:
    def __init__(self, *, response: Any = None, error: Exception | None = None) -> None:
        self.messages = _FakeMessages(response=response, error=error)


def _corrector(client: _FakeClient) -> EntityCorrector:
    return EntityCorrector(api_key="k", model="claude-haiku-4-5", client=client)


async def test_corrects_and_returns_model_output() -> None:
    """A successful call returns the model's corrected text."""
    fixed = "[CUSTOMER] Интересует ЖК Атмосфера на улице Сырым Датулы."
    corrector = _corrector(_FakeClient(response=_response(fixed)))
    assert await corrector.correct(_RAW) == fixed


async def test_prompt_carries_the_glossary() -> None:
    """The system prompt must contain canonical names and Kazakh toponyms."""
    client = _FakeClient(response=_response("ok"))
    await _corrector(client).correct(_RAW)
    system = client.messages.kwargs["system"]
    assert "Атмосфера 2" in system
    assert "Discovery" in system
    assert "Нуршашкан" in system
    assert "Ілияс Жансүгіров" in system
    # The transcript itself goes in the user turn, not the system prompt.
    assert client.messages.kwargs["messages"][0]["content"] == _RAW


async def test_api_error_falls_back_to_raw() -> None:
    """An Anthropic APIError must not propagate; the raw text is kept."""
    from anthropic import APIConnectionError

    err = APIConnectionError(request=httpx.Request("POST", "http://x"))
    corrector = _corrector(_FakeClient(error=err))
    assert await corrector.correct(_RAW) == _RAW


async def test_unexpected_error_falls_back_to_raw() -> None:
    """Any other crash is swallowed and returns the raw text."""
    corrector = _corrector(_FakeClient(error=RuntimeError("boom")))
    assert await corrector.correct(_RAW) == _RAW


async def test_truncation_falls_back_to_raw() -> None:
    """A max_tokens stop reason means truncated output — keep the raw text."""
    resp = _response("[CUSTOMER] Интересует ЖК", stop_reason="max_tokens")
    corrector = _corrector(_FakeClient(response=resp))
    assert await corrector.correct(_RAW) == _RAW


async def test_empty_response_falls_back_to_raw() -> None:
    """An empty model response keeps the raw text."""
    corrector = _corrector(_FakeClient(response=_response("   ")))
    assert await corrector.correct(_RAW) == _RAW


async def test_blank_input_skips_the_api() -> None:
    """Whitespace-only input returns unchanged without calling the model."""
    client = _FakeClient(error=RuntimeError("should not be called"))
    assert await _corrector(client).correct("   ") == "   "
    assert client.messages.kwargs is None
