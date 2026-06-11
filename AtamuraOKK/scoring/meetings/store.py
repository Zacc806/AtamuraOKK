"""Self-contained SQLite state for the meeting-recording pipeline.

Mirrors the call pipeline's status-driven design but in its own SQLite file —
this stays the pipeline's working state; scored results are additionally
mirrored to the shared Postgres ``meetings`` table (see ``push.py``) so the
companion cabinet can read them. One row per Disk recording, keyed by the
Bitrix file id (idempotent re-ingestion):

    NEW → DOWNLOADED → TRANSCRIBED → SCORED ──(pushed_at)──> Postgres
            ↘ SKIPPED (too short / not a meeting)
            ↘ FAILED  (exhausted attempts; see error)
"""

from __future__ import annotations

import enum
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from AtamuraOKK.scoring.meetings.config import config
from AtamuraOKK.scoring.meetings.disk import MeetingFile


class MeetingStatus(enum.StrEnum):
    """Lifecycle status of a meeting recording."""

    NEW = "NEW"
    DOWNLOADED = "DOWNLOADED"
    TRANSCRIBED = "TRANSCRIBED"
    SCORED = "SCORED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    file_id      INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    ext          TEXT,
    size         INTEGER,
    folder_path  TEXT,
    download_url TEXT,
    created_at   TEXT,
    created_by   INTEGER,
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
    pushed_at    TEXT,
    inserted_at  TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_recordings_status ON recordings(status);
"""

# Columns added after the first release; ALTERed in on open so an existing
# meetings.db keeps working without a manual migration step.
_LATER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("created_by", "INTEGER"),
    ("pushed_at", "TEXT"),
)


def resolve_db_path() -> Path:
    """Absolute path to the SQLite state file (under the work dir if relative)."""
    raw = Path(config.meetings_db_path)
    return raw if raw.is_absolute() else config.meetings_work_dir / raw


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


class MeetingStore:
    """Thin SQLite wrapper holding meeting-recording pipeline state."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or resolve_db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        # WAL lets a `status` read run while the worker is mid-write, instead of
        # tripping "database is locked".
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(recordings)")
        }
        for col, col_type in _LATER_COLUMNS:
            if col not in existing:
                self._conn.execute(
                    f"ALTER TABLE recordings ADD COLUMN {col} {col_type}",
                )
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- ingestion ---

    def upsert_new(self, rec: MeetingFile) -> bool:
        """Insert a freshly discovered recording; return True if it was new.

        Existing rows are refreshed only on ingestion-owned columns (name, size,
        download_url, …) so re-ingesting never clobbers pipeline progress.
        """
        now = _now()
        meeting_at = rec.meeting_at.isoformat() if rec.meeting_at else None
        # ``inserted_at`` is set only on INSERT (the DO UPDATE branch leaves it),
        # so comparing it to this call's stamp is a clean "was-new" signal.
        self._conn.execute(
            """
            INSERT INTO recordings (file_id, name, ext, size, folder_path,
                download_url, created_at, created_by, meeting_at, status,
                inserted_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'NEW', ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                name=excluded.name, ext=excluded.ext, size=excluded.size,
                folder_path=excluded.folder_path, download_url=excluded.download_url,
                created_at=excluded.created_at, created_by=excluded.created_by,
                meeting_at=excluded.meeting_at,
                updated_at=excluded.updated_at
            """,
            (
                rec.file_id,
                rec.name,
                rec.ext,
                rec.size,
                rec.folder_path,
                rec.download_url,
                rec.created_at,
                rec.created_by,
                meeting_at,
                now,
                now,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT inserted_at FROM recordings WHERE file_id = ?",
            (rec.file_id,),
        ).fetchone()
        return bool(row) and row["inserted_at"] == now

    # --- stage selection ---

    def claim(self, status: MeetingStatus, limit: int) -> list[sqlite3.Row]:
        """Rows currently in ``status``, oldest meeting first."""
        return list(
            self._conn.execute(
                """
                SELECT * FROM recordings WHERE status = ?
                ORDER BY COALESCE(meeting_at, created_at) ASC, file_id ASC
                LIMIT ?
                """,
                (status.value, limit),
            ).fetchall(),
        )

    def get(self, file_id: int) -> sqlite3.Row | None:
        """Fetch one recording row by id."""
        return self._conn.execute(
            "SELECT * FROM recordings WHERE file_id = ?",
            (file_id,),
        ).fetchone()

    # --- transitions ---

    def _set(self, file_id: int, **fields: Any) -> None:
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(
            f"UPDATE recordings SET {cols} WHERE file_id = ?",  # noqa: S608 (static cols)
            (*fields.values(), file_id),
        )
        self._conn.commit()

    def mark_downloaded(self, file_id: int, audio_path: str, duration_sec: int) -> None:
        """NEW → DOWNLOADED."""
        self._set(
            file_id,
            status=MeetingStatus.DOWNLOADED.value,
            audio_path=audio_path,
            duration_sec=duration_sec,
            error=None,
        )

    def mark_transcribed(self, file_id: int, transcript: str, language: str) -> None:
        """DOWNLOADED → TRANSCRIBED."""
        self._set(
            file_id,
            status=MeetingStatus.TRANSCRIBED.value,
            transcript=transcript,
            language=language,
            error=None,
        )

    def mark_scored(
        self,
        file_id: int,
        score_json: str,
        score_pct: float,
        *,
        passed: bool,
    ) -> None:
        """TRANSCRIBED → SCORED."""
        self._set(
            file_id,
            status=MeetingStatus.SCORED.value,
            score_json=score_json,
            score_pct=score_pct,
            passed=int(passed),
            error=None,
        )

    def mark_skipped(self, file_id: int, reason: str) -> None:
        """Park a recording out of scope (too short / not a meeting)."""
        self._set(file_id, status=MeetingStatus.SKIPPED.value, skip_reason=reason)

    def bump_attempt(self, file_id: int, error: str, *, max_attempts: int) -> bool:
        """Record a failed attempt; flip to FAILED past ``max_attempts``.

        Returns True if the recording was dead-lettered (now FAILED).
        """
        row = self.get(file_id)
        attempts = (row["attempts"] if row else 0) + 1
        failed = attempts >= max_attempts
        fields: dict[str, Any] = {"attempts": attempts, "error": error}
        if failed:
            fields["status"] = MeetingStatus.FAILED.value
        self._set(file_id, **fields)
        return failed

    def reset_failed(self) -> int:
        """Re-queue every FAILED recording for another attempt; return the count.

        Each row resumes at the furthest stage its data supports — a stored
        transcript → TRANSCRIBED, else a present audio file → DOWNLOADED, else
        NEW — with the error and attempt counter cleared.
        """
        rows = self._conn.execute(
            "SELECT * FROM recordings WHERE status = ?",
            (MeetingStatus.FAILED.value,),
        ).fetchall()
        for row in rows:
            if row["transcript"]:
                status = MeetingStatus.TRANSCRIBED
            elif row["audio_path"] and Path(row["audio_path"]).exists():
                status = MeetingStatus.DOWNLOADED
            else:
                status = MeetingStatus.NEW
            self._set(int(row["file_id"]), status=status.value, attempts=0, error=None)
        return len(rows)

    def reset_for_rescore(self, *, min_transcript_chars: int | None = None) -> int:
        """SCORED → TRANSCRIBED so the next pass re-scores (and re-pushes).

        Clears the score and the ``pushed_at`` marker — the Postgres upsert
        then refreshes the same ``meetings`` row. With ``min_transcript_chars``
        only rows whose transcript exceeds it are reset (e.g. meetings scored
        while long transcripts were still being truncated).
        """
        where = "status = ?"
        params: list[Any] = [MeetingStatus.SCORED.value]
        if min_transcript_chars is not None:
            where += " AND LENGTH(COALESCE(transcript, '')) > ?"
            params.append(min_transcript_chars)
        rows = self._conn.execute(
            f"SELECT file_id FROM recordings WHERE {where}",  # noqa: S608 (static)
            params,
        ).fetchall()
        for row in rows:
            self._set(
                int(row["file_id"]),
                status=MeetingStatus.TRANSCRIBED.value,
                score_json=None,
                score_pct=None,
                passed=None,
                pushed_at=None,
                error=None,
                attempts=0,
            )
        return len(rows)

    # --- Postgres push ---

    def claim_unpushed(self, limit: int) -> list[sqlite3.Row]:
        """SCORED rows not yet mirrored to Postgres, oldest meeting first."""
        return list(
            self._conn.execute(
                """
                SELECT * FROM recordings
                WHERE status = ? AND pushed_at IS NULL
                ORDER BY COALESCE(meeting_at, created_at) ASC, file_id ASC
                LIMIT ?
                """,
                (MeetingStatus.SCORED.value, limit),
            ).fetchall(),
        )

    def mark_pushed(self, file_id: int) -> None:
        """Record that this recording's score now lives in Postgres too."""
        self._set(file_id, pushed_at=_now())

    # --- reporting ---

    def counts(self) -> dict[str, int]:
        """Recording count grouped by status."""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM recordings GROUP BY status",
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def scored(self) -> list[sqlite3.Row]:
        """All SCORED recordings, newest meeting first."""
        return list(
            self._conn.execute(
                """
                SELECT * FROM recordings WHERE status = ?
                ORDER BY COALESCE(meeting_at, created_at) DESC, file_id DESC
                """,
                (MeetingStatus.SCORED.value,),
            ).fetchall(),
        )


@contextmanager
def open_store(db_path: Path | None = None) -> Iterator[MeetingStore]:
    """Context-managed :class:`MeetingStore`."""
    store = MeetingStore(db_path)
    try:
        yield store
    finally:
        store.close()
