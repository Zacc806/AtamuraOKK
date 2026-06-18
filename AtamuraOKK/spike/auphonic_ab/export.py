"""Turn ``results.json`` into a human-readable A/B report.

Emits ``out/summary.csv`` (one row per call, before-vs-after at a glance) and
``out/<id>.md`` (the two transcripts side by side so a reviewer can judge whether
cleanup actually improved the words). Quality here is qualitative + proxy metrics
(segment/char counts, detected language, per-segment language mix) — there is no
ground-truth reference, so this is a comparison aid, not an automatic WER verdict.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from loguru import logger

from AtamuraOKK.spike.auphonic_ab import config

_SUMMARY_COLUMNS = [
    "id",
    "direction",
    "prior_status",
    "prior_language",
    "duration_sec",
    "orig_channels",
    "clean_channels",
    "auphonic_status",
    "before_ok",
    "before_lang",
    "before_segs",
    "before_chars",
    "after_ok",
    "after_lang",
    "after_segs",
    "after_chars",
    "chars_delta",
]


def _row(rec: dict[str, Any]) -> dict[str, Any]:
    orig = rec.get("orig") or {}
    clean = rec.get("clean") or {}
    before = rec.get("before") or {}
    after = rec.get("after") or {}
    bc = before.get("n_chars") or 0
    ac = after.get("n_chars") or 0
    return {
        "id": rec.get("id"),
        "direction": rec.get("direction"),
        "prior_status": rec.get("prior_status"),
        "prior_language": rec.get("prior_language"),
        "duration_sec": rec.get("duration_sec"),
        "orig_channels": orig.get("channels"),
        "clean_channels": clean.get("channels"),
        "auphonic_status": clean.get("status_string") or clean.get("error"),
        "before_ok": before.get("ok"),
        "before_lang": before.get("language"),
        "before_segs": before.get("n_segments"),
        "before_chars": bc,
        "after_ok": after.get("ok"),
        "after_lang": after.get("language"),
        "after_segs": after.get("n_segments"),
        "after_chars": ac,
        "chars_delta": ac - bc,
    }


def _transcript_text(call_id: int, side: str) -> str:
    path = config.TRANSCRIPT_DIR / f"{call_id}.{side}.json"
    if not path.exists():
        return "_(no transcript)_"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("full_text") or "_(empty)_"


def _seg_lang_str(side: dict[str, Any]) -> str:
    langs = side.get("seg_languages") or {}
    return ", ".join(f"{k}:{v}" for k, v in sorted(langs.items())) or "-"


def _markdown(rec: dict[str, Any]) -> str:
    cid = rec["id"]
    orig = rec.get("orig") or {}
    clean = rec.get("clean") or {}
    before = rec.get("before") or {}
    after = rec.get("after") or {}
    lines = [
        f"# Call {cid} — A/B (Auphonic cleanup)",
        "",
        f"- bitrix_call_id: `{rec.get('bitrix_call_id')}`",
        f"- direction: {rec.get('direction')} | duration: {rec.get('duration_sec')}s"
        f" | prior status: {rec.get('prior_status')}"
        f" | prior lang: {rec.get('prior_language')}",
        f"- channels: orig={orig.get('channels')} → clean={clean.get('channels')}"
        f" | Auphonic: {clean.get('status_string') or clean.get('error')}"
        f" ({clean.get('auphonic_uuid', 'n/a')})",
        "",
        "| metric | BEFORE | AFTER |",
        "|---|---|---|",
        f"| ok | {before.get('ok')} | {after.get('ok')} |",
        f"| language | {before.get('language')} | {after.get('language')} |",
        f"| segments | {before.get('n_segments')} | {after.get('n_segments')} |",
        f"| chars | {before.get('n_chars')} | {after.get('n_chars')} |",
        f"| seg langs | {_seg_lang_str(before)} | {_seg_lang_str(after)} |",
    ]
    if before.get("error") or after.get("error"):
        lines.append(
            f"| error | {before.get('error') or '-'} | {after.get('error') or '-'} |"
        )
    lines += [
        "",
        "## BEFORE (original audio)",
        "",
        "```",
        _transcript_text(cid, "before"),
        "```",
        "",
        "## AFTER (Auphonic-cleaned audio)",
        "",
        "```",
        _transcript_text(cid, "after"),
        "```",
        "",
    ]
    return "\n".join(lines)


def _is_complete(rec: dict[str, Any]) -> bool:
    """A call with a successful A/B: cleanup ran and an 'after' transcript exists."""
    clean = rec.get("clean") or {}
    return "error" not in clean and bool(clean) and "after" in rec


def _write_report(records: list[dict[str, Any]], out_dir: Path) -> None:
    """Write summary.csv + per-call markdown for ``records`` into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_SUMMARY_COLUMNS)
        writer.writeheader()
        for rec in records:
            writer.writerow(_row(rec))
    for rec in records:
        (out_dir / f"{rec['id']}.md").write_text(_markdown(rec), encoding="utf-8")


def export(ready_only: bool = False) -> None:
    """Write the A/B report from results.json.

    Default: every non-fatal record into ``out/``. With ``ready_only`` only the
    fully-completed calls (cleanup succeeded + transcribed) go into ``out/ready/``
    — the shareable subset when a run was cut short (e.g. out of Auphonic credits).
    """
    records: list[dict[str, Any]] = json.loads(
        config.RESULTS.read_text(encoding="utf-8")
    )
    usable = [r for r in records if "fatal" not in r]

    if ready_only:
        ready = [r for r in usable if _is_complete(r)]
        out_dir = config.OUT_DIR / "ready"
        _write_report(ready, out_dir)
        print(  # noqa: T201 - spike CLI summary
            f"\nExported {len(ready)} completed calls -> {out_dir}\n"
            f"(skipped {len(usable) - len(ready)} calls without a finished cleanup)"
        )
        logger.info("Exported {n} ready calls -> {p}", n=len(ready), p=out_dir)
        return

    _write_report(usable, config.OUT_DIR)
    _print_overview(records)
    logger.info("Exported A/B report -> {p}", p=config.OUT_DIR)


def _print_overview(records: list[dict[str, Any]]) -> None:
    rescued = sum(
        1
        for r in records
        if (r.get("before") or {}).get("ok") is False
        and (r.get("after") or {}).get("ok") is True
    )
    stereo_lost = sum(
        1
        for r in records
        if (r.get("orig") or {}).get("channels", 0) >= 2
        and (r.get("clean") or {}).get("channels", 0) < 2
    )
    cleaned = sum(1 for r in records if "channels" in (r.get("clean") or {}))
    print(  # noqa: T201 - spike CLI summary
        f"\nA/B overview: {len(records)} calls | cleaned OK: {cleaned} | "
        f"rescued (before-fail→after-ok): {rescued} | "
        f"stereo lost in cleanup: {stereo_lost}\n"
        f"See {config.OUT_DIR}/summary.csv and per-call .md files."
    )
