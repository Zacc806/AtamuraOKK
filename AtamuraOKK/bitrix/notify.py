"""Bitrix24 personal notifications — the project's only *write* to Bitrix.

Everything else in this package is read-only (telephony/CRM pulls). This sends a
manager a personal notification via ``im.notify.personal.add`` (needs the ``im``
scope on the inbound webhook). Pluggable like ``ops.alert``: a logging fallback is
used until ``bitrix_notify_enabled`` is set, so notifications can never fire by
accident.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from loguru import logger

from AtamuraOKK.bitrix.client import BitrixClient
from AtamuraOKK.settings import settings


@runtime_checkable
class BitrixNotifier(Protocol):
    """Sends a personal notification to a Bitrix user."""

    async def send(self, user_id: int, message: str) -> bool:
        """Deliver ``message`` to the Bitrix user ``user_id``; True on success."""
        ...


class LogNotifier:
    """Fallback that logs instead of writing to Bitrix (the default)."""

    async def send(self, user_id: int, message: str) -> bool:
        """Log the notification."""
        logger.info(
            "Bitrix notify disabled — would notify user {uid}:\n{msg}",
            uid=user_id,
            msg=message,
        )
        return True


class BitrixImNotifier:
    """Sends a personal Bitrix notification via ``im.notify.personal.add``."""

    async def send(self, user_id: int, message: str) -> bool:
        """POST one personal notification; return True on success."""
        async with BitrixClient() as bx:
            await bx.call(
                "im.notify.personal.add",
                {"USER_ID": user_id, "MESSAGE": message},
            )
        return True


def get_notifier() -> BitrixNotifier:
    """Real Bitrix notifier when enabled, else the logging fallback."""
    if settings.bitrix_notify_enabled:
        return BitrixImNotifier()
    return LogNotifier()
