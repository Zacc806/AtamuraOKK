"""Drive the A/B run: fetch -> transcribe before -> clean -> transcribe after.

Per call we keep both audio renditions and both transcripts on disk, plus a
compact metrics record (``results.json``) the export step turns into the A/B
report. Each call is isolated: any failure (download, transcription, Auphonic)
is captured against that call and the run continues — a "before" failure that
becomes an "after" success is exactly the rescue signal we're looking for.

Transcription uses the production Yandex SpeechKit v3 async provider directly
(``transcribe_file``), so segmentation, channel/speaker handling, and the
ru-RU+kk-KZ language whitelist match prod exactly.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from AtamuraOKK.audio import probe_channels
from AtamuraOKK.spike.auphonic_ab import config
from AtamuraOKK.spike.auphonic_ab.auphonic import AuphonicClient, AuphonicError
from AtamuraOKK.spike.auphonic_ab.select import CallRef, load_manifest
from AtamuraOKK.storage import get_storage
from AtamuraOKK.transcription.base import TranscriptResult
from AtamuraOKK.transcription.language import detect_language
from AtamuraOKK.transcription.yandex_async_provider import YandexAsyncTranscriber


@dataclass(slots=True)
class _Side:
    """Metrics for one transcription (before or after cleanup)."""

    ok: bool
    error: str | None = None
    language: str | None = None
    n_segments: int = 0
    n_chars: int = 0
    speakers: dict[str, int] | None = None  # speaker -> char count
    channels: int | None = None
    seg_languages: dict[str, int] | None = None  # per-segment detected language


def _metrics(res: TranscriptResult, channels: int) -> _Side:
    speakers: Counter[str] = Counter()
    seg_langs: Counter[str] = Counter()
    for seg in res.segments:
        speakers[seg.speaker] += len(seg.text)
        if seg.text.strip():
            seg_langs[detect_language(seg.text)] += 1
    return _Side(
        ok=True,
        language=res.language,
        n_segments=len(res.segments),
        n_chars=len(res.full_text),
        speakers=dict(speakers),
        channels=channels,
        seg_languages=dict(seg_langs),
    )


async def _transcribe(
    transcriber: YandexAsyncTranscriber, audio: Path, out_json: Path
) -> _Side:
    try:
        channels = probe_channels(audio)
        res = await transcriber.transcribe_file(audio)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(
            json.dumps(res.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return _metrics(res, channels)
    except Exception as exc:
        logger.warning("  transcription failed for {a}: {e}", a=audio.name, e=exc)
        return _Side(ok=False, error=f"{type(exc).__name__}: {exc}")


async def _process_one(
    ref: CallRef,
    transcriber: YandexAsyncTranscriber,
    auphonic: AuphonicClient,
    storage: Any,
) -> dict[str, Any]:
    cid = ref.id
    logger.info("[{cid}] {key}", cid=cid, key=ref.audio_object_key)
    record: dict[str, Any] = {
        "id": cid,
        "bitrix_call_id": ref.bitrix_call_id,
        "duration_sec": ref.duration_sec,
        "direction": ref.direction,
        "prior_status": ref.prior_status,
        "prior_language": ref.prior_language,
    }

    orig = config.AUDIO_DIR / f"{cid}.orig.mp3"
    clean = config.AUDIO_DIR / f"{cid}.clean.mp3"
    orig.parent.mkdir(parents=True, exist_ok=True)

    # 1) Fetch original audio from object storage.
    orig.write_bytes(await storage.download(ref.audio_object_key))
    orig_channels = probe_channels(orig)
    record["orig"] = {"bytes": orig.stat().st_size, "channels": orig_channels}

    # 2) Transcribe BEFORE.
    before = await _transcribe(
        transcriber, orig, config.TRANSCRIPT_DIR / f"{cid}.before.json"
    )
    record["before"] = asdict(before)

    # 3) Auphonic cleanup.
    try:
        result = await auphonic.process(orig, title=f"call-{cid}", dest=clean)
        clean_channels = probe_channels(clean)
        record["clean"] = {
            "bytes": clean.stat().st_size,
            "channels": clean_channels,
            "auphonic_uuid": result.uuid,
            "status_string": result.status_string,
        }
    except (AuphonicError, OSError) as exc:
        logger.warning("[{cid}] Auphonic failed: {e}", cid=cid, e=exc)
        record["clean"] = {"error": f"{type(exc).__name__}: {exc}"}
        record["after"] = asdict(_Side(ok=False, error="cleanup unavailable"))
        return record

    # 4) Transcribe AFTER.
    after = await _transcribe(
        transcriber, clean, config.TRANSCRIPT_DIR / f"{cid}.after.json"
    )
    record["after"] = asdict(after)
    return record


def _is_complete(record: dict[str, Any]) -> bool:
    """A record needs no rework: cleanup succeeded and an 'after' exists."""
    clean = record.get("clean") or {}
    return "error" not in clean and bool(clean) and "after" in record


def _load_prior() -> dict[int, dict[str, Any]]:
    """Read an existing results.json (for --resume), keyed by call id."""
    if not config.RESULTS.exists():
        return {}
    prior = json.loads(config.RESULTS.read_text(encoding="utf-8"))
    return {r["id"]: r for r in prior if "id" in r}


async def run(
    limit: int | None = None,
    concurrency: int = 4,
    *,
    resume: bool = False,
) -> list[dict[str, Any]]:
    """Process the manifest, up to ``concurrency`` calls at once.

    Each call is independent (own audio, own Auphonic production, own STT
    requests), so they parallelize cleanly under a semaphore. Results are still
    persisted incrementally and in manifest order after every completion, so a
    mid-run interruption keeps finished work regardless of completion order.

    With ``resume=True`` any call already complete in ``results.json`` (cleanup
    succeeded + transcribed) is kept as-is and skipped — so topping up Auphonic
    credits and re-running only reprocesses the calls that previously errored.
    """
    refs = load_manifest()
    if limit is not None:
        refs = refs[:limit]
    transcriber = YandexAsyncTranscriber()
    auphonic = AuphonicClient()
    storage = get_storage()

    sem = asyncio.Semaphore(max(1, concurrency))
    write_lock = asyncio.Lock()
    by_id: dict[int, dict[str, Any]] = {}
    order = [ref.id for ref in refs]
    done = 0

    if resume:
        prior = _load_prior()
        by_id = {cid: r for cid, r in prior.items() if _is_complete(r)}
        refs = [ref for ref in refs if ref.id not in by_id]
        done = len(by_id)
        logger.info("Resume: {k} already complete, {r} to process", k=done, r=len(refs))

    async def worker(ref: CallRef) -> None:
        nonlocal done
        async with sem:
            try:
                record = await _process_one(ref, transcriber, auphonic, storage)
            except Exception as exc:
                logger.error("[{cid}] fatal: {e}", cid=ref.id, e=exc)
                record = {"id": ref.id, "fatal": f"{type(exc).__name__}: {exc}"}
        async with write_lock:
            by_id[ref.id] = record
            done += 1
            logger.info(
                "--- {d}/{n} done (call {cid}) ---", d=done, n=len(order), cid=ref.id
            )
            ordered = [by_id[cid] for cid in order if cid in by_id]
            config.RESULTS.write_text(
                json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    logger.info("Processing {n} calls, concurrency={c}", n=len(refs), c=concurrency)
    await asyncio.gather(*(worker(ref) for ref in refs))

    records = [by_id[cid] for cid in order if cid in by_id]
    logger.info("Wrote {n} records -> {p}", n=len(records), p=config.RESULTS)
    return records
