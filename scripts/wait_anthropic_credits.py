"""Watch for Anthropic credits to return, then resume scoring.

Probes the API with a 1-token request every 10 minutes. On success:
restarts the docker `score` worker and requeues credit-failure FAILED calls
(attempts reset, status back to TRANSCRIBED) so the dispatcher rescores them.
"""

from __future__ import annotations

import asyncio
import subprocess

from anthropic import AsyncAnthropic, BadRequestError
from loguru import logger
from sqlalchemy import update

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.settings import settings

POLL_SECONDS = 600
CREDIT_SIGNATURE = "credit balance is too low"


async def probe() -> bool:
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        await client.messages.create(
            model=settings.anthropic_scoring_model,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
    except BadRequestError as exc:
        if CREDIT_SIGNATURE in str(exc):
            return False
        raise  # anything else is a real bug, not a billing wait
    return True


async def main() -> None:
    while not await probe():
        logger.warning("Anthropic credits still exhausted — sleeping {s}s", s=POLL_SECONDS)
        await asyncio.sleep(POLL_SECONDS)

    async with session_scope() as session:
        result = await session.execute(
            update(Call)
            .where(
                Call.status == CallStatus.FAILED,
                Call.error.like(f"%{CREDIT_SIGNATURE}%"),
            )
            .values(status=CallStatus.TRANSCRIBED, attempts=0, error=None, claimed_at=None)
        )
    subprocess.run(
        ["docker", "start", "atamuraokk-score-1"],
        check=True, capture_output=True, timeout=120,
    )
    logger.info(
        "Anthropic credits restored: score worker started, {n} calls requeued",
        n=result.rowcount or 0,
    )


if __name__ == "__main__":
    asyncio.run(main())
