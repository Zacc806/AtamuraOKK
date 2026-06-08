"""Tests for the meeting-recording pipeline stages (fakes, no network/ffmpeg)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from AtamuraOKK.scoring.meetings import download as download_mod
from AtamuraOKK.scoring.meetings import recordings, transcribe
from AtamuraOKK.scoring.meetings.base import CallForScoring, ScoreResult
from AtamuraOKK.scoring.meetings.config import config
from AtamuraOKK.scoring.meetings.disk import MeetingDiskSource, MeetingFile
from AtamuraOKK.scoring.meetings.store import MeetingStatus, MeetingStore
from AtamuraOKK.scoring.meetings.transcribe import TranscriptText


def _file(
    file_id: int = 1, *, name: str = "rec.ogg", url: str | None = "u"
) -> MeetingFile:
    """Build a MeetingFile fixture."""
    return MeetingFile(
        file_id=file_id,
        name=name,
        ext=".ogg",
        size=10,
        folder_path="Май",
        download_url=url,
        created_at="2026-06-03T10:00:00+03:00",
        meeting_at=datetime(2025, 5, 31, 15, 0, 0),
    )


class _Resp:
    """Minimal httpx-like response."""

    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        """No-op status check."""
        return


class _FakeHTTP:
    """Fake async HTTP client returning canned bytes."""

    def __init__(self, content: bytes = b"audio-bytes") -> None:
        self._content = content
        self.urls: list[str] = []

    async def get(self, url: str) -> _Resp:
        """Record the URL and return canned bytes."""
        self.urls.append(url)
        return _Resp(self._content)


class _FakeDisk:
    """Fake disk exposing only ``call`` (for disk.file.get) + ``aclose``."""

    def __init__(self, file_info: dict[str, Any] | None = None) -> None:
        self._file_info = file_info or {}

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        """Return the canned disk.file.get payload."""
        return self._file_info

    async def aclose(self) -> None:  # pragma: no cover
        """No-op close."""
        return


# --- download ---


async def test_download_happy_path(tmp_path: Path, monkeypatch: Any) -> None:
    """A NEW recording is fetched to disk and advanced to DOWNLOADED."""
    monkeypatch.setattr(config, "meetings_work_dir", tmp_path)
    monkeypatch.setattr(download_mod, "probe_duration_sec", lambda _p: 130)
    store = MeetingStore(tmp_path / "m.db")
    store.upsert_new(_file())
    http = _FakeHTTP()

    stats = await download_mod.download_pending(
        store=store,
        disk=_FakeDisk(),  # type: ignore[arg-type]
        http=http,  # type: ignore[arg-type]
    )

    assert stats.downloaded == 1
    row = store.get(1)
    assert row["status"] == MeetingStatus.DOWNLOADED.value
    assert row["duration_sec"] == 130
    assert Path(row["audio_path"]).read_bytes() == b"audio-bytes"
    store.close()


async def test_download_skips_too_short(tmp_path: Path, monkeypatch: Any) -> None:
    """A sub-minute clip is parked as SKIPPED, not downloaded."""
    monkeypatch.setattr(config, "meetings_work_dir", tmp_path)
    monkeypatch.setattr(download_mod, "probe_duration_sec", lambda _p: 5)
    store = MeetingStore(tmp_path / "m.db")
    store.upsert_new(_file())

    stats = await download_mod.download_pending(
        store=store,
        disk=_FakeDisk(),  # type: ignore[arg-type]
        http=_FakeHTTP(),  # type: ignore[arg-type]
    )

    assert stats.skipped == 1
    assert store.get(1)["status"] == MeetingStatus.SKIPPED.value
    store.close()


async def test_download_resolves_url_when_missing(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """When the row has no URL, it is resolved via disk.file.get."""
    monkeypatch.setattr(config, "meetings_work_dir", tmp_path)
    monkeypatch.setattr(download_mod, "probe_duration_sec", lambda _p: 90)
    store = MeetingStore(tmp_path / "m.db")
    store.upsert_new(_file(url=None))
    http = _FakeHTTP()
    disk = _FakeDisk({"DOWNLOAD_URL": "resolved-url"})

    await download_mod.download_pending(
        store=store,
        disk=disk,  # type: ignore[arg-type]
        http=http,  # type: ignore[arg-type]
    )

    assert http.urls == ["resolved-url"]
    assert store.get(1)["status"] == MeetingStatus.DOWNLOADED.value
    store.close()


# --- transcribe ---


class _FakeTranscriber:
    """Fake transcriber returning a fixed text + language."""

    def __init__(self, text: str = "[agent] привет [customer] здравствуйте") -> None:
        self._text = text

    async def transcribe(self, wav_path: Path) -> TranscriptText:
        """Return the canned transcript."""
        return TranscriptText(text=self._text, language="ru")


async def test_transcribe_marks_transcribed(tmp_path: Path, monkeypatch: Any) -> None:
    """A DOWNLOADED recording becomes TRANSCRIBED with stored text."""
    audio = tmp_path / "1.ogg"
    audio.write_bytes(b"x")
    monkeypatch.setattr(transcribe, "to_mono_wav", lambda src, dest: src)
    store = MeetingStore(tmp_path / "m.db")
    store.upsert_new(_file())
    store.mark_downloaded(1, str(audio), 120)

    stats = await transcribe.transcribe_pending(
        store=store,
        transcriber=_FakeTranscriber(),
    )

    assert stats.transcribed == 1
    row = store.get(1)
    assert row["status"] == MeetingStatus.TRANSCRIBED.value
    assert "привет" in row["transcript"]
    assert row["language"] == "ru"
    store.close()


async def test_transcribe_empty_text_bumps_attempt(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """An empty transcript does not advance the row; it records an attempt."""
    audio = tmp_path / "1.ogg"
    audio.write_bytes(b"x")
    monkeypatch.setattr(transcribe, "to_mono_wav", lambda src, dest: src)
    store = MeetingStore(tmp_path / "m.db")
    store.upsert_new(_file())
    store.mark_downloaded(1, str(audio), 120)

    await transcribe.transcribe_pending(
        store=store,
        transcriber=_FakeTranscriber(text="   "),
    )

    row = store.get(1)
    assert row["status"] == MeetingStatus.DOWNLOADED.value  # not advanced
    assert row["attempts"] == 1
    store.close()


# --- ingest ---


class _TreeDisk:
    """Fake disk exposing ``children`` over a canned tree."""

    def __init__(self, tree: dict[int, list[dict[str, Any]]]) -> None:
        self._tree = tree

    async def children(self, folder_id: int) -> list[dict[str, Any]]:
        """Return canned children."""
        return self._tree.get(folder_id, [])

    async def aclose(self) -> None:  # pragma: no cover
        """No-op close."""
        return


async def test_ingest_registers_new(tmp_path: Path) -> None:
    """Ingestion registers only audio/video files as NEW."""
    tree = {
        7: [
            {
                "ID": "1",
                "TYPE": "file",
                "NAME": "a 2025-05-01 at 09.00.00.ogg",
                "SIZE": "5",
            },
            {"ID": "2", "TYPE": "file", "NAME": "ignore.png"},
        ],
    }
    source = MeetingDiskSource(_TreeDisk(tree), root_id=7)  # type: ignore[arg-type]
    store = MeetingStore(tmp_path / "m.db")

    stats = await recordings.ingest_recordings(store=store, source=source)

    assert (stats.scanned, stats.new) == (1, 1)
    assert [r["file_id"] for r in store.claim(MeetingStatus.NEW, 10)] == [1]
    store.close()


# --- score ---


def _fake_result() -> ScoreResult:
    """A minimal okk_meeting_v1 ScoreResult."""
    return ScoreResult(
        rubric_version="okk_meeting_v1",
        total_score=40,
        max_total=50,
        score_pct=80.0,
        passed=True,
        criteria=[],
        call_type="первичный",
        client_agreed_meeting=True,
        manager_tone="вежливый",
        red_flags=[],
        summary="ок",
        language="ru",
        provider="anthropic",
        model="claude",
    )


class _FakeScorer:
    """Fake meeting scorer recording the calls it scored."""

    def __init__(self) -> None:
        self.seen: list[CallForScoring] = []

    async def score(self, call: CallForScoring) -> ScoreResult:
        """Record the call and return a fixed result."""
        self.seen.append(call)
        return _fake_result()


async def test_score_pending_persists_result(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """A TRANSCRIBED recording is scored and persisted as SCORED."""
    scorer = _FakeScorer()
    monkeypatch.setattr(recordings, "build_meeting_scorer", lambda: scorer)
    store = MeetingStore(tmp_path / "m.db")
    store.upsert_new(_file())
    store.mark_downloaded(1, "/a.ogg", 120)
    store.mark_transcribed(1, "[agent] привет", "ru")

    stats = await recordings.score_pending(store=store)

    assert stats.scored == 1
    row = store.get(1)
    assert row["status"] == MeetingStatus.SCORED.value
    assert row["score_pct"] == 80.0
    assert row["passed"] == 1
    assert scorer.seen[0].text == "[agent] привет"
    assert scorer.seen[0].duration_sec == 120
    store.close()


async def test_run_pipeline_wires_all_stages(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """run_pipeline calls every stage in order and returns counts."""
    monkeypatch.setattr(config, "meetings_work_dir", tmp_path)
    monkeypatch.setattr(config, "meetings_db_path", "run.db")
    called: list[str] = []

    async def _ing(**_kw: Any) -> recordings.IngestStats:
        called.append("ingest")
        return recordings.IngestStats(scanned=0, new=0)

    async def _dl(**_kw: Any) -> download_mod.DownloadStats:
        called.append("download")
        return download_mod.DownloadStats()

    async def _tr(**_kw: Any) -> transcribe.TranscribeStats:
        called.append("transcribe")
        return transcribe.TranscribeStats()

    async def _sc(**_kw: Any) -> recordings.ScoreStats:
        called.append("score")
        return recordings.ScoreStats()

    monkeypatch.setattr(recordings, "ingest_recordings", _ing)
    monkeypatch.setattr(recordings, "download_pending", _dl)
    monkeypatch.setattr(recordings, "transcribe_pending", _tr)
    monkeypatch.setattr(recordings, "score_pending", _sc)

    result = await recordings.run_pipeline()

    assert called == ["ingest", "download", "transcribe", "score"]
    assert "counts" in result
