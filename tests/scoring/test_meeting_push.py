"""Tests for the SCORED → Postgres push stage (companion visibility).

Bitrix enrichment is stubbed out (no network in tests), exercising the
placeholder-manager fallback that production uses when ``user.get`` is
unavailable.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.db.models.meeting import Meeting
from AtamuraOKK.scoring.meetings.disk import MeetingFile
from AtamuraOKK.scoring.meetings.push import push_pending
from AtamuraOKK.scoring.meetings.store import MeetingStore

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _no_bitrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the placeholder-manager fallback (never hit the network)."""

    def _raise(*args: Any, **kwargs: Any) -> Any:
        msg = "no Bitrix in tests"
        raise ValueError(msg)

    monkeypatch.setattr("AtamuraOKK.bitrix.BitrixClient", _raise)


def _file(file_id: int = 1, *, created_by: int | None = 901) -> MeetingFile:
    return MeetingFile(
        file_id=file_id,
        name=f"WhatsApp Audio 2026-05-31 at 15.58.0{file_id}.mp4",
        ext=".mp4",
        size=1234,
        folder_path="Встречи ОП/Май",
        download_url="https://x/download",
        created_at="2026-06-03T10:00:00+03:00",
        meeting_at=datetime(2026, 5, 31, 15, 58, 2),
        created_by=created_by,
    )


def _score_json(pct: float = 80.0) -> str:
    return json.dumps(
        {
            "rubric_version": "okk_meeting_v1",
            "total_score": 40,
            "max_total": 50,
            "score_pct": pct,
            "passed": pct >= 75,
            "criteria": [
                {
                    "id": 1,
                    "block": "Контакт",
                    "name": "Приветствие",
                    "score": 4,
                    "max_score": 5,
                    "auto": False,
                },
            ],
            "call_type": "первичный",
            "client_agreed_meeting": True,
            "manager_tone": "вежливый",
            "red_flags": ["перебивал клиента"],
            "summary": "Встреча прошла хорошо.",
            "language": "ru",
            "provider": "anthropic",
            "model": "test",
            "needs_human_review": False,
        },
        ensure_ascii=False,
    )


def _seed_scored(
    store: MeetingStore,
    file_id: int = 1,
    *,
    created_by: int | None = 901,
) -> None:
    store.upsert_new(_file(file_id, created_by=created_by))
    store.mark_downloaded(file_id, f"/audio/{file_id}.mp4", 1800)
    store.mark_transcribed(file_id, "[agent] добрый день", "ru")
    store.mark_scored(file_id, _score_json(), 80.0, passed=True)


async def test_push_mirrors_scored_meeting(
    dbsession: AsyncSession,
    tmp_path: Path,
) -> None:
    """A SCORED row lands in Postgres, attributed to the uploader's manager row."""
    with MeetingStore(tmp_path / "m.db") as store:
        _seed_scored(store)
        stats = await push_pending(store=store, session=dbsession)

        assert (stats.attempted, stats.pushed, stats.failed) == (1, 1, 0)
        assert store.get(1)["pushed_at"] is not None

    meeting = await dbsession.scalar(
        select(Meeting).where(Meeting.bitrix_file_id == 1),
    )
    assert meeting is not None
    assert meeting.source == "op"
    assert meeting.uploaded_by_bitrix_id == 901
    assert float(meeting.score_pct) == 80.0
    assert meeting.passed is True
    assert meeting.call_type == "первичный"
    assert meeting.manager_tone == "вежливый"
    assert meeting.red_flags == ["перебивал клиента"]
    assert meeting.meeting_at is not None
    assert meeting.score["criteria"][0]["name"] == "Приветствие"

    # The uploader got a placeholder managers row, linked on the meeting.
    manager = await dbsession.scalar(
        select(Manager).where(Manager.bitrix_user_id == 901),
    )
    assert manager is not None
    assert meeting.manager_id == manager.id


async def test_push_links_existing_manager(
    dbsession: AsyncSession,
    tmp_path: Path,
) -> None:
    """An already-known uploader is linked, not duplicated."""
    mgr = Manager(bitrix_user_id=901, name="Айгерим")
    dbsession.add(mgr)
    await dbsession.flush()

    with MeetingStore(tmp_path / "m.db") as store:
        _seed_scored(store)
        await push_pending(store=store, session=dbsession)

    meeting = await dbsession.scalar(
        select(Meeting).where(Meeting.bitrix_file_id == 1),
    )
    assert meeting is not None
    assert meeting.manager_id == mgr.id
    managers = (
        await dbsession.scalars(select(Manager).where(Manager.bitrix_user_id == 901))
    ).all()
    assert len(managers) == 1


async def test_push_without_uploader(
    dbsession: AsyncSession,
    tmp_path: Path,
) -> None:
    """A recording with no CREATED_BY still pushes, unattributed."""
    with MeetingStore(tmp_path / "m.db") as store:
        _seed_scored(store, created_by=None)
        stats = await push_pending(store=store, session=dbsession)
        assert stats.pushed == 1

    meeting = await dbsession.scalar(
        select(Meeting).where(Meeting.bitrix_file_id == 1),
    )
    assert meeting is not None
    assert meeting.uploaded_by_bitrix_id is None
    assert meeting.manager_id is None


async def test_push_is_once_and_upsert_idempotent(
    dbsession: AsyncSession,
    tmp_path: Path,
) -> None:
    """Pushed rows are not re-sent; a forced re-push updates the same row."""
    with MeetingStore(tmp_path / "m.db") as store:
        _seed_scored(store)
        await push_pending(store=store, session=dbsession)

        second = await push_pending(store=store, session=dbsession)
        assert (second.attempted, second.pushed) == (0, 0)

        # Re-score → clear pushed_at → re-push updates, never duplicates.
        store.mark_scored(1, _score_json(90.0), 90.0, passed=True)
        store._set(1, pushed_at=None)  # noqa: SLF001
        third = await push_pending(store=store, session=dbsession)
        assert third.pushed == 1

    meetings = (
        await dbsession.scalars(select(Meeting).where(Meeting.bitrix_file_id == 1))
    ).all()
    assert len(meetings) == 1
    assert float(meetings[0].score_pct) == 90.0


async def test_push_skips_bad_score_payload(
    dbsession: AsyncSession,
    tmp_path: Path,
) -> None:
    """A malformed score_json row is counted failed and doesn't block the batch."""
    with MeetingStore(tmp_path / "m.db") as store:
        _seed_scored(store, 1)
        _seed_scored(store, 2)
        store._set(2, score_json="{not json")  # noqa: SLF001

        stats = await push_pending(store=store, session=dbsession)
        assert (stats.attempted, stats.pushed, stats.failed) == (2, 1, 1)
        assert store.get(1)["pushed_at"] is not None
        assert store.get(2)["pushed_at"] is None


def test_store_migrates_old_schema(tmp_path: Path) -> None:
    """Opening a pre-push-era meetings.db adds the new columns in place."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE recordings (
            file_id      INTEGER PRIMARY KEY,
            name         TEXT NOT NULL,
            ext          TEXT,
            size         INTEGER,
            folder_path  TEXT,
            download_url TEXT,
            created_at   TEXT,
            meeting_at   TEXT,
            status       TEXT NOT NULL DEFAULT 'NEW',
            audio_path   TEXT,
            duration_sec INTEGER,
            transcript   TEXT,
            language     TEXT,
            score_json   TEXT,
            score_pct    REAL,
            passed       INTEGER,
            skip_reason  TEXT,
            error        TEXT,
            attempts     INTEGER NOT NULL DEFAULT 0,
            inserted_at  TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        INSERT INTO recordings (file_id, name, status, inserted_at, updated_at)
        VALUES (7, 'old.ogg', 'SCORED', '2026-01-01', '2026-01-01');
        """,
    )
    conn.commit()
    conn.close()

    with MeetingStore(db) as store:
        row = store.get(7)
        assert row["created_by"] is None
        assert row["pushed_at"] is None
        assert [r["file_id"] for r in store.claim_unpushed(10)] == [7]
        store.mark_pushed(7)
        assert store.claim_unpushed(10) == []
