"""One-off eval: transcribe a sample of April 2026 calls with Yandex -> JSON.

April predates the corpus (ingestion starts 2026-05-01) and is driven by a
shared production cursor, so this script does NOT ingest or touch any DB / the
cursor. It pulls April calls **directly** from Bitrix over an explicit date
window, downloads each recording to a temp file, transcribes it with the
configured engine (Yandex SpeechKit v3 async), and writes the transcripts to a
JSON file. Nothing is persisted to Postgres or object storage.

April recordings are pre-stereo-rollout (mostly mono); the Yandex async engine
falls back to speaker-labeling for mono, so segments are still speaker-split.

Run from the repo root:
    uv run python scripts/april_yandex_eval.py                 # ~15 calls
    uv run python scripts/april_yandex_eval.py --count 20 --scan-cap 5000
    uv run python scripts/april_yandex_eval.py -o exports/april.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import httpx
from loguru import logger

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.ingestion.mapping import to_call_fields
from AtamuraOKK.ingestion.service import _is_answered_recorded
from AtamuraOKK.transcription.base import TranscriptResult
from AtamuraOKK.transcription.factory import get_transcriber
from AtamuraOKK.transcription.worker import _load_transcriber, _transcribe_audio

# April 2026 in Asia/Qyzylorda (+05), expressed as UTC instants (matches the
# boundary convention in scripts/backfill_may.py).
APRIL_START = datetime(2026, 3, 31, 19, 0, tzinfo=UTC)  # 2026-04-01 00:00 +05
APRIL_END = datetime(2026, 4, 30, 19, 0, tzinfo=UTC)  # 2026-05-01 00:00 +05

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "exports" / "april_2026_yandex_transcripts.json"


async def _collect_candidates(bx: BitrixClient, scan_cap: int) -> list[dict]:
    """Scan April calls (ASC) and keep answered + recorded ones, deduped."""
    params = {
        "FILTER": {
            ">=CALL_START_DATE": APRIL_START.isoformat(),
            "<CALL_START_DATE": APRIL_END.isoformat(),
        },
        "ORDER": {"CALL_START_DATE": "ASC"},
    }
    seen: set[str] = set()
    candidates: list[dict] = []
    scanned = 0
    async for row in bx.list("voximplant.statistic.get", params, max_items=scan_cap):
        scanned += 1
        if not _is_answered_recorded(row):
            continue
        call_id = str(row.get("CALL_ID") or "")
        if not call_id or call_id in seen:
            continue
        seen.add(call_id)
        candidates.append(row)
    logger.info(
        "Scanned {s} April rows; {c} answered+recorded candidates",
        s=scanned,
        c=len(candidates),
    )
    return candidates


def _evenly_sample(rows: list[dict], count: int) -> list[dict]:
    """Pick `count` rows spread evenly across the scanned candidate list."""
    if len(rows) <= count:
        return rows
    step = len(rows) / count
    return [rows[int(i * step)] for i in range(count)]


async def _resolve_url(row: dict, bx: BitrixClient) -> str | None:
    """Direct recording URL, resolving a Disk file id when no inline URL."""
    url = row.get("CALL_RECORD_URL") or None
    if url:
        return url
    file_id = row.get("RECORD_FILE_ID")
    if file_id:
        info = await bx.call("disk.file.get", {"id": int(file_id)})
        if info:
            return info.get("DOWNLOAD_URL")
    return None


async def _transcribe_call(
    row: dict,
    transcriber,
    bx: BitrixClient,
    http: httpx.AsyncClient,
) -> dict | None:
    """Download one recording to a temp file and transcribe it; build a record."""
    fields = to_call_fields(row)
    call_id = fields["bitrix_call_id"]
    started = fields.get("started_at")
    try:
        url = await _resolve_url(row, bx)
        if not url:
            logger.warning("call {id}: no recording url", id=call_id)
            return None
        resp = await http.get(url)
        resp.raise_for_status()
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            src = tmpdir / f"{call_id}.mp3"
            src.write_bytes(resp.content)
            result: TranscriptResult = await _transcribe_audio(
                transcriber, src, tmpdir
            )
    except (BitrixError, httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        logger.warning("call {id}: failed ({e})", id=call_id, e=exc)
        return None

    logger.info(
        "call {id} ({d}s) -> lang={lang} segs={n} chars={c}",
        id=call_id,
        d=fields.get("duration_sec"),
        lang=result.language,
        n=len(result.segments),
        c=len(result.full_text),
    )
    return {
        "bitrix_call_id": call_id,
        "started_at": started.isoformat() if started else None,
        "duration_sec": fields.get("duration_sec"),
        "direction": getattr(fields.get("direction"), "value", None),
        "phone_number": fields.get("phone_number"),
        "portal_user_id": fields.get("portal_user_id"),
        "language": result.language,
        "model": result.model,
        "stereo": result.meta.get("stereo"),
        "channels": result.meta.get("channels"),
        "segment_count": len(result.segments),
        "segments": [asdict(s) for s in result.segments],
        "full_text": result.full_text,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=15, help="calls to transcribe")
    parser.add_argument(
        "--scan-cap",
        type=int,
        default=4000,
        help="max April rows to scan for candidates (ASC from Apr 1)",
    )
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    transcriber = await asyncio.to_thread(_load_transcriber)

    async with (
        BitrixClient() as bx,
        httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http,
    ):
        candidates = await _collect_candidates(bx, args.scan_cap)
        if not candidates:
            logger.error("No April candidates found — nothing to do.")
            return
        chosen = _evenly_sample(candidates, args.count)
        logger.info(
            "Transcribing {n} of {c} candidates (concurrency={k})",
            n=len(chosen),
            c=len(candidates),
            k=args.concurrency,
        )

        sem = asyncio.Semaphore(args.concurrency)

        async def one(row: dict) -> dict | None:
            async with sem:
                return await _transcribe_call(row, transcriber, bx, http)

        results = await asyncio.gather(*(one(r) for r in chosen))

    records = [r for r in results if r]
    records.sort(key=lambda r: r["started_at"] or "")
    payload = {
        "generated_for": "2026-04",
        "provider": "yandex",
        "model": records[0]["model"] if records else None,
        "window": {"start": APRIL_START.isoformat(), "end": APRIL_END.isoformat()},
        "scanned_cap": args.scan_cap,
        "candidates": len(candidates),
        "requested": args.count,
        "transcribed": len(records),
        "calls": records,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "Wrote {n} transcript(s) -> {p}",
        n=len(records),
        p=args.out,
    )


if __name__ == "__main__":
    asyncio.run(main())
