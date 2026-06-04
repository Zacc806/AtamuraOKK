"""Pluggable alerting: Telegram when configured, otherwise log."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx
from loguru import logger

from AtamuraOKK.settings import settings


@runtime_checkable
class Alerter(Protocol):
    """Sends an operational alert/notification."""

    async def send(self, text: str) -> bool:
        """Deliver ``text``; return True on success."""
        ...


class LogAlerter:
    """Fallback that logs the alert (used when Telegram isn't configured)."""

    async def send(self, text: str) -> bool:
        """Log the alert."""
        logger.warning("ALERT (no Telegram configured):\n{text}", text=text)
        return True


class TelegramAlerter:
    """Send alerts to a Telegram chat via a bot."""

    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id

    async def send(self, text: str) -> bool:
        """POST the message to the Telegram Bot API."""
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=20.0) as http:
                resp = await http.post(
                    url,
                    json={
                        "chat_id": self._chat_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                )
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Telegram alert failed: {e}", e=exc)
            return False
        return True


def get_alerter() -> Alerter:
    """Telegram alerter if configured, else the logging fallback."""
    if settings.telegram_bot_token and settings.telegram_chat_id:
        return TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
    return LogAlerter()
