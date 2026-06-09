"""Download meeting recordings from the Disk into the local work dir.

NEW → DOWNLOADED (or SKIPPED when the audio is shorter than the meeting floor).
Self-contained: writes audio under ``meetings_work_dir/audio`` and records state
in the meetings SQLite — no object storage, no Postgres.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
from loguru import logger

from AtamuraOKK.scoring.meetings.config import config
from AtamuraOKK.scoring.meetings.disk import BitrixDisk
from AtamuraOKK.scoring.meetings.media import probe_duration_sec
from AtamuraOKK.scoring.meetings.store import MeetingStatus, MeetingStore


@dataclass
class DownloadStats:
    """Summary of one download pass."""

    attempted: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0


def _audio_dir() -> Path:
    path = config.meetings_work_dir / "audio"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _resolve_url(row_url: str | None, file_id: int, disk: BitrixDisk) -> str:
    if row_url:
        return row_url
    info = await disk.call("disk.file.get", {"id": file_id})
    url = (info or {}).get("DOWNLOAD_URL")
    if not url:
        raise ValueError("no DOWNLOAD_URL for disk file")
    return str(url)


async def download_pending(
    *,
    limit: int | None = None,
    store: MeetingStore | None = None,
    disk: BitrixDisk | None = None,
    http: httpx.AsyncClient | None = None,
) -> DownloadStats:
    """Fetch NEW recordings to local audio storage; park sub-minute clips."""
    stats = DownloadStats()
    limit = limit if limit is not None else config.meetings_batch_limit
    own_store = store is None
    own_disk = disk is None
    own_http = http is None
    # Construct owned resources inside try so a constructor failure (e.g. a bad
    # webhook URL) can't leak an already-opened client/connection.
    try:
        store = store or MeetingStore()
        disk = disk or BitrixDisk()
        http = http or httpx.AsyncClient(timeout=300.0, follow_redirects=True)
        audio_dir = _audio_dir()
        for row in store.claim(MeetingStatus.NEW, limit):
            stats.attempted += 1
            file_id = int(row["file_id"])
            dest = audio_dir / f"{file_id}{row['ext'] or ''}"
            try:
                url = await _resolve_url(row["download_url"], file_id, disk)
                resp = await http.get(url)
                resp.raise_for_status()
                _atomic_write(dest, resp.content)
                duration = probe_duration_sec(dest)
                if duration and duration < config.meetings_min_duration_sec:
                    dest.unlink(missing_ok=True)
                    store.mark_skipped(file_id, f"too_short:{duration}s")
                    stats.skipped += 1
                    continue
                store.mark_downloaded(file_id, str(dest), duration)
                stats.downloaded += 1
            except (httpx.HTTPError, ValueError, OSError) as exc:
                dead = store.bump_attempt(
                    file_id,
                    f"download: {exc}",
                    max_attempts=config.meetings_max_attempts,
                )
                stats.failed += int(dead)
                logger.warning(
                    "Meeting download failed for {id}: {e}", id=file_id, e=exc
                )
    finally:
        if own_http and http is not None:
            await http.aclose()
        if own_disk and disk is not None:
            await disk.aclose()
        if own_store and store is not None:
            store.close()

    logger.info(
        "Meeting download: attempted={a} downloaded={d} skipped={s} failed={f}",
        a=stats.attempted,
        d=stats.downloaded,
        s=stats.skipped,
        f=stats.failed,
    )
    return stats


def _atomic_write(dest: Path, data: bytes) -> None:
    """Write bytes via a temp file + rename so partial downloads never persist."""
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".part")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        tmp_path.replace(dest)
    except OSError:
        tmp_path.unlink(missing_ok=True)  # don't orphan a half-written .part
        raise
