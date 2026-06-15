#!/usr/bin/env python3
"""Download + transcribe a scoped subset of ОП meeting recordings.

The normal ``download``/``transcribe`` CLIs drain the whole backlog oldest-first
(763 rows). This runner restricts both stages to a selectable subset so we
transcribe only what's wanted without touching (or paying STT for) the rest. It
reuses the pipeline's real building blocks — the same Disk client, audio prep,
transcriber, and SQLite transitions — so the resulting state is identical to a
normal pipeline run, just scoped.

    # every real meeting recording, skipping WhatsApp voice notes:
    uv run python scripts/transcribe_month_meetings.py --exclude-whatsapp
    # a single month (by meeting_at, fallback upload time):
    uv run python scripts/transcribe_month_meetings.py --month 2026-05

Requires ffmpeg (downmix to OGG/Opus for Yandex STT) and the `yandex` dep group.
Idempotent: rows already past a stage are skipped, so re-running only finishes
what's left. Unlike the normal download stage this does NOT skip short clips —
the goal is a transcript for every selected meeting.
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

import httpx
from loguru import logger

from AtamuraOKK.scoring.meetings.config import config
from AtamuraOKK.scoring.meetings.disk import BitrixDisk
from AtamuraOKK.scoring.meetings.download import _atomic_write, _resolve_url
from AtamuraOKK.scoring.meetings.media import probe_duration_sec
from AtamuraOKK.scoring.meetings.store import MeetingStatus, MeetingStore
from AtamuraOKK.scoring.meetings.transcribe import _prepare_audio, build_transcriber


def _where(month: str | None, exclude_whatsapp: bool) -> tuple[str, list[object]]:
    """SQL predicate + params for the selected subset (excludes status)."""
    clauses: list[str] = []
    params: list[object] = []
    if month:
        clauses.append("substr(COALESCE(meeting_at, created_at), 1, 7) = ?")
        params.append(month)
    if exclude_whatsapp:
        clauses.append("LOWER(name) NOT LIKE '%whatsapp%'")
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _select_ids(
    store: MeetingStore,
    status: MeetingStatus,
    month: str | None,
    exclude_whatsapp: bool,
) -> list[int]:
    """File ids in ``status`` matching the subset, oldest first."""
    where, params = _where(month, exclude_whatsapp)
    rows = store._conn.execute(  # noqa: SLF001 (one-off script, same repo)
        f"""
        SELECT file_id FROM recordings
        WHERE status = ?{where}
        ORDER BY COALESCE(meeting_at, created_at) ASC, file_id ASC
        """,  # noqa: S608 (where is built from static fragments, params bound)
        (status.value, *params),
    ).fetchall()
    return [int(r["file_id"]) for r in rows]


async def _download(store: MeetingStore, month: str | None, no_wa: bool) -> None:
    ids = _select_ids(store, MeetingStatus.NEW, month, no_wa)
    logger.info("Download: {n} NEW recordings selected", n=len(ids))
    if not ids:
        return
    audio_dir = config.meetings_work_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    async with BitrixDisk() as disk:
        async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as http:
            for file_id in ids:
                row = store.get(file_id)
                if row is None:
                    continue
                dest = audio_dir / f"{file_id}{row['ext'] or ''}"
                try:
                    url = await _resolve_url(row["download_url"], file_id, disk)
                    resp = await http.get(url)
                    resp.raise_for_status()
                    _atomic_write(dest, resp.content)
                    duration = probe_duration_sec(dest)
                    store.mark_downloaded(file_id, str(dest), duration)
                    logger.info("  downloaded {id} ({d}s)", id=file_id, d=duration)
                except (httpx.HTTPError, ValueError, OSError) as exc:
                    store.bump_attempt(
                        file_id,
                        f"download: {exc}",
                        max_attempts=config.meetings_max_attempts,
                    )
                    logger.warning("  download failed {id}: {e}", id=file_id, e=exc)


async def _transcribe(store: MeetingStore, month: str | None, no_wa: bool) -> None:
    ids = _select_ids(store, MeetingStatus.DOWNLOADED, month, no_wa)
    logger.info("Transcribe: {n} DOWNLOADED recordings selected", n=len(ids))
    if not ids:
        return
    transcriber = build_transcriber()
    suffix = getattr(transcriber, "audio_suffix", ".wav")
    sem = asyncio.Semaphore(max(1, config.meetings_transcribe_concurrency))

    async def _one(file_id: int) -> None:
        row = store.get(file_id)
        if row is None:
            return
        audio_path = row["audio_path"]
        try:
            async with sem:
                if not audio_path or not Path(audio_path).exists():
                    raise FileNotFoundError(f"audio missing: {audio_path}")
                with tempfile.TemporaryDirectory() as tmp:
                    src = await asyncio.to_thread(
                        _prepare_audio, Path(audio_path), Path(tmp), suffix=suffix
                    )
                    result = await transcriber.transcribe(src)
            if not result.text.strip():
                raise ValueError("empty transcript")
            store.mark_transcribed(file_id, result.text, result.language)
            logger.info(
                "  transcribed {id} ({c} chars)", id=file_id, c=len(result.text)
            )
        except Exception as exc:
            store.bump_attempt(
                file_id,
                f"transcribe: {exc}",
                max_attempts=config.meetings_max_attempts,
            )
            logger.warning("  transcribe failed {id}: {e}", id=file_id, e=exc)

    await asyncio.gather(*(_one(i) for i in ids))


async def _run(month: str | None, no_wa: bool) -> dict[str, int]:
    with MeetingStore() as store:
        await _download(store, month, no_wa)
        await _transcribe(store, month, no_wa)
        where, params = _where(month, no_wa)
        rows = store._conn.execute(  # noqa: SLF001
            f"SELECT status, COUNT(*) n FROM recordings WHERE 1=1{where} GROUP BY status",  # noqa: E501, S608
            params,
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--month", default=None, help="YYYY-MM filter (default: all)")
    p.add_argument(
        "--exclude-whatsapp",
        action="store_true",
        help="skip WhatsApp voice notes (keep only real meeting recordings)",
    )
    args = p.parse_args()
    counts = asyncio.run(_run(args.month, args.exclude_whatsapp))
    logger.info("Selected-subset final status counts: {c}", c=counts)


if __name__ == "__main__":
    main()
