"""Tests for the Bitrix Disk meeting source (filename parsing + A/V walk)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from AtamuraOKK.scoring.meetings.disk import MeetingDiskSource, parse_meeting_time


def test_parse_meeting_time_whatsapp() -> None:
    """The WhatsApp 'YYYY-MM-DD at HH.MM.SS' pattern parses to a datetime."""
    got = parse_meeting_time("WhatsApp Audio 2025-09-05 at 11.05.22.mp4")
    assert got == datetime(2025, 9, 5, 11, 5, 22)


def test_parse_meeting_time_date_only() -> None:
    """A bare date in the filename parses to midnight."""
    assert parse_meeting_time("встреча 2025-05-31.ogg") == datetime(2025, 5, 31)


def test_parse_meeting_time_compact() -> None:
    """A compact 'YYYYMMDD_HHMMSS' stamp parses too."""
    assert parse_meeting_time("REC_20250531_155802.m4a") == datetime(
        2025,
        5,
        31,
        15,
        58,
        2,
    )


def test_parse_meeting_time_absent_or_invalid() -> None:
    """No stamp / an out-of-range stamp yields None."""
    assert parse_meeting_time("audio.ogg") is None
    assert parse_meeting_time("2025-13-40 at 99.99.99.mp4") is None


class _FakeDisk:
    """Returns a canned folder tree for ``children`` calls."""

    def __init__(self, tree: dict[int, list[dict[str, Any]]]) -> None:
        self._tree = tree
        self.calls: list[int] = []

    async def children(self, folder_id: int) -> list[dict[str, Any]]:
        """Return the canned children of a folder."""
        self.calls.append(folder_id)
        return self._tree.get(folder_id, [])

    async def aclose(self) -> None:  # pragma: no cover - interface parity
        """No-op close."""
        return


def _folder(fid: int, name: str) -> dict[str, Any]:
    """A folder child payload."""
    return {"ID": str(fid), "TYPE": "folder", "NAME": name}


def _file(fid: int, name: str, **extra: Any) -> dict[str, Any]:
    """A file child payload."""
    return {"ID": str(fid), "TYPE": "file", "NAME": name, **extra}


async def test_iter_recordings_filters_av_and_recurses() -> None:
    """Only audio/video files are yielded; subfolders are walked."""
    tree = {
        804938: [
            _folder(100, "Май"),
            _file(1, "junk.png"),
            _file(
                2,
                "voice 2025-05-31 at 10.00.00.ogg",
                SIZE="111",
                DOWNLOAD_URL="u2",
                CREATE_TIME="2026-06-03T10:00:00+03:00",
            ),
        ],
        100: [
            _file(3, "WhatsApp Audio 2025-09-05 at 11.05.22.mp4", SIZE="222"),
            _file(4, "scan.pdf"),
            _file(5, "photo.jpeg"),
        ],
    }
    disk = _FakeDisk(tree)
    source = MeetingDiskSource(disk, root_id=804938)  # type: ignore[arg-type]

    recs = [r async for r in source.iter_recordings()]

    by_id = {r.file_id: r for r in recs}
    assert set(by_id) == {2, 3}  # only the audio/video files
    assert by_id[2].ext == ".ogg"
    assert by_id[2].download_url == "u2"
    assert by_id[3].folder_path == "Май"
    assert by_id[3].meeting_at == datetime(2025, 9, 5, 11, 5, 22)


async def test_iter_recordings_respects_max_items() -> None:
    """max_items caps how many recordings are yielded."""
    tree = {
        1: [
            _file(10, "a 2025-05-01 at 09.00.00.ogg"),
            _file(11, "b 2025-05-02 at 09.00.00.ogg"),
            _file(12, "c 2025-05-03 at 09.00.00.ogg"),
        ],
    }
    source = MeetingDiskSource(_FakeDisk(tree), root_id=1)  # type: ignore[arg-type]
    recs = [r async for r in source.iter_recordings(max_items=2)]
    assert len(recs) == 2


async def test_iter_recordings_honours_max_depth(monkeypatch: Any) -> None:
    """A depth cap of 0 keeps the walk from descending into subfolders."""
    from AtamuraOKK.scoring.meetings import disk as disk_mod

    monkeypatch.setattr(disk_mod.config, "meetings_walk_max_depth", 0)
    tree = {
        1: [_folder(2, "deep"), _file(10, "top 2025-05-01 at 09.00.00.ogg")],
        2: [_file(11, "buried 2025-05-02 at 09.00.00.ogg")],
    }
    source = MeetingDiskSource(_FakeDisk(tree), root_id=1)  # type: ignore[arg-type]
    recs = [r async for r in source.iter_recordings()]
    assert {r.file_id for r in recs} == {10}
