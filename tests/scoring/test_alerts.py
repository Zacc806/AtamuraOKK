"""Tests for manipulation admin alerts (log + optional Telegram)."""

from __future__ import annotations

from typing import Any

import pytest

from AtamuraOKK.scoring.meetings import alerts
from AtamuraOKK.scoring.meetings.manipulation import Manipulation


def _spy_post(calls: list[Any]) -> Any:
    def _post(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    return _post


def test_no_manipulations_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty input sends nothing."""
    calls: list[Any] = []
    monkeypatch.setattr(alerts.httpx, "post", _spy_post(calls))
    alerts.notify_manipulations("call1", [])
    assert calls == []


def test_unconfigured_telegram_skips_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without token/chat_id it logs only — no Telegram HTTP call."""
    calls: list[Any] = []
    monkeypatch.setattr(alerts.httpx, "post", _spy_post(calls))
    monkeypatch.setattr(alerts.settings, "telegram_bot_token", "")
    monkeypatch.setattr(alerts.settings, "telegram_alert_chat_id", "")

    alerts.notify_manipulations(
        "call1",
        [Manipulation(zhk="Аура", claim="лифт", reality="нет", severity="high")],
    )
    assert calls == []


def test_configured_telegram_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    """With token + chat_id set, one Telegram message is posted."""
    calls: list[Any] = []
    monkeypatch.setattr(alerts.httpx, "post", _spy_post(calls))
    monkeypatch.setattr(alerts.settings, "telegram_bot_token", "tok")
    monkeypatch.setattr(alerts.settings, "telegram_alert_chat_id", "42")

    alerts.notify_manipulations(
        "call1",
        [Manipulation(zhk="Аура", claim="лифт", reality="нет", severity="high")],
    )
    assert len(calls) == 1
