"""Ingestion job: pull recent calls from Bitrix and upsert them."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from AtamuraOKK.bitrix import BitrixClient
from AtamuraOKK.db.dao.call_dao import CallDAO
from AtamuraOKK.db.dao.ingest_state_dao import IngestStateDAO
from AtamuraOKK.db.dao.manager_dao import ManagerDAO
from AtamuraOKK.ingestion.mapper import MappedCall, map_row
from AtamuraOKK.settings import settings

_SOURCE = "voximplant"


def _to_values(mapped: MappedCall, manager_id: int | None) -> dict[str, Any]:
    return {
        "bitrix_call_id": mapped.bitrix_call_id,
        "manager_id": manager_id,
        "direction": mapped.direction,
        "started_at": mapped.started_at,
        "duration_sec": mapped.duration_sec,
        "failed_code": mapped.failed_code,
        "record_file_id": mapped.record_file_id,
        "record_url": mapped.record_url,
        "crm_entity_type": mapped.crm_entity_type,
        "crm_entity_id": mapped.crm_entity_id,
        "crm_activity_id": mapped.crm_activity_id,
        "phone_number": mapped.phone_number,
        "status": mapped.status,
    }


def _window_start(last_window_end: datetime | None) -> datetime:
    overlap = timedelta(hours=settings.ingest_window_overlap_hours)
    if last_window_end is not None:
        return last_window_end - overlap
    return datetime.now(UTC) - timedelta(days=settings.ingest_days_back)


async def run_ingest(factory: async_sessionmaker[AsyncSession]) -> int:
    """Pull calls since the cursor window and upsert them. Returns rows seen."""
    async with BitrixClient() as bx, factory() as session:
        states = IngestStateDAO(session)
        calls = CallDAO(session)
        managers = ManagerDAO(session)

        state = await states.get(_SOURCE)
        window_start = _window_start(state.last_window_end if state else None)
        manager_ids = await managers.id_map()
        max_id = state.last_call_id if state else 0

        params = {
            "FILTER": {">=CALL_START_DATE": window_start.strftime("%Y-%m-%dT%H:%M:%S")},
        }
        seen = 0
        async for row in bx.list("voximplant.statistic.get", params):
            mapped = map_row(row, min_duration_sec=settings.ingest_min_duration_sec)
            if not mapped.bitrix_call_id:
                continue
            manager_id = (
                manager_ids.get(mapped.bitrix_user_id)
                if mapped.bitrix_user_id is not None
                else None
            )
            await calls.upsert_from_bitrix(_to_values(mapped, manager_id))
            with contextlib.suppress(ValueError):
                max_id = max(max_id, int(mapped.bitrix_call_id))
            seen += 1

        window_end = datetime.now(UTC) - timedelta(
            hours=settings.ingest_window_overlap_hours,
        )
        await states.advance(_SOURCE, last_call_id=max_id, last_window_end=window_end)
        await session.commit()
        logger.info("ingest: upserted {n} calls since {s}", n=seen, s=window_start)
        return seen
