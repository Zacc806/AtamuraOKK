"""C4 regression: the ingestion cursor is timezone-correct.

The cursor is parsed/compared as an aware datetime and emitted as ISO-with-offset.
A lexicographic string compare of raw ``CALL_START_DATE`` values (the old bug)
could advance the cursor past unseen earlier calls and skip them permanently.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any, ClassVar, Self

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.ingest_state import IngestState
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.ingestion import service
from AtamuraOKK.ingestion.qualification import NullQualificationChecker
from AtamuraOKK.ingestion.service import _parse_cursor, _since_filter
from AtamuraOKK.settings import settings

# 10:00Z vs 09:30-05:00 (=14:30Z): chronologically B is later, but as raw strings
# "...T10:00:00+00:00" > "...T09:30:00-05:00" lexicographically — the trap.
_EARLIER = "2026-06-09T10:00:00+00:00"
_LATER = "2026-06-09T09:30:00-05:00"


def test_parse_cursor_keeps_offset() -> None:
    """A stored cursor with an offset is parsed as an aware datetime."""
    dt = _parse_cursor("2026-06-09T10:00:00+03:00")
    assert dt is not None
    assert dt.utcoffset() == timedelta(hours=3)


def test_parse_cursor_treats_naive_as_utc() -> None:
    """An older naive cursor is interpreted as UTC, not left naive."""
    dt = _parse_cursor("2026-06-09T10:00:00")
    assert dt is not None
    assert dt.utcoffset() == timedelta(0)


def test_parse_cursor_handles_garbage_and_none() -> None:
    """Unparseable / missing cursors return None."""
    assert _parse_cursor("not-a-date") is None
    assert _parse_cursor(None) is None


def test_since_filter_emits_offset_and_subtracts_overlap() -> None:
    """The Bitrix lower bound keeps its offset and backs off by the overlap."""
    out = _since_filter("2026-06-09T10:00:00+00:00")
    parsed = datetime.fromisoformat(out)
    assert parsed.utcoffset() == timedelta(0)
    expected = datetime.fromisoformat("2026-06-09T10:00:00+00:00") - timedelta(
        minutes=settings.ingest_overlap_minutes,
    )
    assert parsed == expected


def test_since_filter_default_is_aware() -> None:
    """With no cursor the lower bound is still timezone-aware."""
    out = _since_filter(None)
    assert datetime.fromisoformat(out).tzinfo is not None


def _row(call_id: str, started: str) -> dict[str, Any]:
    return {
        "CALL_ID": call_id,
        "CALL_TYPE": "1",
        "CALL_START_DATE": started,
        "CALL_DURATION": 600,
        "CALL_FAILED_CODE": settings.ingest_success_code,
        "CALL_RECORD_URL": "http://rec/x.wav",
        "CRM_ENTITY_TYPE": "CONTACT",
        "CRM_ENTITY_ID": 4242,
    }


class _FakeBitrix:
    rows: ClassVar[list[dict[str, Any]]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        for row in type(self).rows:
            yield row


async def _cleanup() -> None:
    async with session_scope() as session:
        await session.execute(
            delete(Call).where(Call.bitrix_call_id.like("cursor-%")),
        )
        await session.execute(
            delete(IngestState).where(IngestState.key == service.CURSOR_KEY),
        )


@pytest.fixture
async def _clean(_engine: AsyncEngine) -> AsyncIterator[None]:
    await _cleanup()
    try:
        yield
    finally:
        await _cleanup()


async def test_cursor_advances_to_latest_instant_not_lexicographic_max(
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ingestion stores the chronologically latest call, not the largest string."""
    _FakeBitrix.rows = [
        _row("cursor-A", _EARLIER),
        _row("cursor-B", _LATER),
    ]
    monkeypatch.setattr(service, "BitrixClient", _FakeBitrix)

    stats = await service.run_ingestion(checker=NullQualificationChecker())

    assert stats.upserted == 2
    # The chronologically latest call (B, 14:30Z) wins — not the lexicographically
    # larger string (A).
    assert stats.cursor == _LATER
    async with session_scope() as session:
        state = await session.scalar(
            select(IngestState).where(IngestState.key == service.CURSOR_KEY),
        )
    assert state is not None
    assert state.last_cursor == _LATER
