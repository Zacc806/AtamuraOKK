"""Admin alerts for the manipulation detector (ТЗ 2.1): log + optional Telegram.

Always logs a warning per detected manipulation; additionally posts to Telegram
when ``telegram_bot_token`` + ``telegram_alert_chat_id`` are configured (the ТЗ
asks for a Telegram alert to the OKK admin). No-op when nothing was detected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from loguru import logger

from AtamuraOKK.settings import settings

if TYPE_CHECKING:
    from AtamuraOKK.scoring.meetings.manipulation import Manipulation

_TELEGRAM_API = "https://api.telegram.org"
_TIMEOUT_SEC = 10.0


def notify_manipulations(call_ref: str, manipulations: list[Manipulation]) -> None:
    """Log each manipulation and, if configured, send one Telegram alert."""
    if not manipulations:
        return
    for m in manipulations:
        logger.warning(
            "MANIPULATION ref={ref} zhk={z} severity={s}: {c} | reality: {r}",
            ref=call_ref,
            z=m.zhk,
            s=m.severity,
            c=m.claim,
            r=m.reality,
        )
    _send_telegram(call_ref, manipulations)


def _send_telegram(call_ref: str, manipulations: list[Manipulation]) -> None:
    token = settings.telegram_bot_token
    chat_id = settings.telegram_alert_chat_id
    if not token or not chat_id:
        return
    lines = [f"[ОКК] Манипуляции во встрече {call_ref}:"]
    lines += [
        f"- [{m.severity}] {m.zhk}: {m.claim} (реальность: {m.reality})"
        for m in manipulations
    ]
    try:
        httpx.post(
            f"{_TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "\n".join(lines)},
            timeout=_TIMEOUT_SEC,
        )
    except httpx.HTTPError as exc:  # network boundary: log, don't break scoring
        logger.warning("telegram alert failed for {ref}: {e}", ref=call_ref, e=exc)
