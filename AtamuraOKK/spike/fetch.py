"""Stage 1: pull a sample of recent *answered* calls with recordings.

Writes ``<spike_dir>/calls.json`` — the working set the later stages consume.
Only telephony scope is required for this stage.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from AtamuraOKK.bitrix import BitrixClient
from AtamuraOKK.settings import settings

# A call counts as "answered and recorded" when Bitrix reports success and
# attaches a recording file. CALL_RECORD_URL is empty on this portal (external
# telephony integration); recordings are Bitrix Drive files in RECORD_FILE_ID.
SUCCESS_CODE = "200"
MIN_DURATION_SEC = 15


async def fetch_sample(
    *,
    sample_size: int = 60,
    days_back: int = 7,
) -> list[dict[str, Any]]:
    """Collect up to ``sample_size`` recent answered+recorded calls."""
    cutoff = (datetime.now(tz=UTC) - timedelta(days=days_back)).strftime(
        "%Y-%m-%dT%H:%M:%S",
    )
    params = {
        "FILTER": {">=CALL_START_DATE": cutoff},
        "ORDER": {"CALL_START_DATE": "DESC"},  # honored best-effort by Bitrix
    }

    collected: list[dict[str, Any]] = []
    scanned = 0
    async with BitrixClient() as bx:
        async for row in bx.list("voximplant.statistic.get", params):
            scanned += 1
            if row.get("CALL_FAILED_CODE") != SUCCESS_CODE:
                continue
            if int(row.get("CALL_DURATION") or 0) < MIN_DURATION_SEC:
                continue
            if not row.get("RECORD_FILE_ID") and not row.get("CALL_RECORD_URL"):
                continue
            collected.append(row)
            if len(collected) >= sample_size:
                break

    logger.info(
        "Scanned {scanned} calls since {cutoff}; kept {kept} answered+recorded.",
        scanned=scanned,
        cutoff=cutoff,
        kept=len(collected),
    )
    return collected


def save(calls: list[dict[str, Any]]) -> None:
    """Persist the working set to ``<spike_dir>/calls.json``."""
    settings.spike_dir.mkdir(parents=True, exist_ok=True)
    out = settings.spike_dir / "calls.json"
    out.write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote {n} calls to {path}", n=len(calls), path=out)
