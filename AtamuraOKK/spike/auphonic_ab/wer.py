"""Ground-truth WER/CER harness for the A/B spike.

The char-count / similarity analysis can't say whether cleanup made transcripts
*better* — only *different*. This adds the defensible metric: word- and
character-error rate of each rendition against a human reference.

Flow:

1. ``label-init`` picks a subset of the completed calls (Kazakh + stereo first,
   where cleanup changed the most) and writes a **blank** reference template per
   call under ``labels/<id>.ref.txt`` plus a ``labels/INDEX.md`` pointing at the
   original audio. Blank (not pre-filled) so the reference doesn't anchor on
   either the before or after transcript — that bias would rig the comparison.
2. A human listens to ``<id>.orig.mp3`` and types what was actually said into the
   ref file (lines starting with ``#`` are ignored).
3. ``wer`` scores before/after against every filled-in reference with jiwer and
   reports which rendition is closer to the truth.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import jiwer
from loguru import logger

from AtamuraOKK.spike.auphonic_ab import config

LABELS_DIR = config.WORK_DIR / "labels"
DEFAULT_COUNT = 12

_TEMPLATE = """\
# Reference transcript for call {cid}  (lang={lang}, channels={ch}, {dur}s)
# Audio to transcribe: {audio}
# Type EXACTLY what is said (both speakers), plain text, punctuation optional.
# Lines starting with '#' are ignored. Leave nothing below if you skip this call.
"""


def _normalize(text: str) -> str:
    """Label-stripped, lowercased, punctuation-free, whitespace-collapsed text."""
    text = re.sub(r"\[[A-Za-z]+\]", " ", text)  # drop [SPEAKER] markers
    text = text.lower().replace("ё", "е")  # noqa: RUF001 - intentional Cyrillic
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _ref_body(path: Path) -> str:
    """Reference text from a template file (non-comment, non-blank lines)."""
    lines = [
        ln
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    return _normalize(" ".join(lines))


def _complete(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        r
        for r in records
        if "error" not in (r.get("clean") or {})
        and (r.get("clean") or {})
        and "after" in r
    ]


def _priority(rec: dict[str, Any]) -> tuple[int, int]:
    """Sort key: Kazakh+stereo first, then stereo, then Kazakh; shorter first."""
    ch = (rec.get("orig") or {}).get("channels") or 1
    kk = rec.get("prior_language") == "kk"
    stereo = ch >= 2
    tier = 0 if (kk and stereo) else 1 if stereo else 2 if kk else 3
    return (tier, rec.get("duration_sec") or 0)


def label_init(count: int = DEFAULT_COUNT, *, prefill: bool = False) -> None:
    """Write blank reference templates for the top-``count`` priority calls."""
    records = json.loads(config.RESULTS.read_text(encoding="utf-8"))
    chosen = sorted(_complete(records), key=_priority)[:count]
    LABELS_DIR.mkdir(parents=True, exist_ok=True)

    index = ["# Labeling worklist (fill each <id>.ref.txt, then run `wer`)", ""]
    created = kept = 0
    for rec in chosen:
        cid = rec["id"]
        ch = (rec.get("orig") or {}).get("channels")
        audio = (config.AUDIO_DIR / f"{cid}.orig.mp3").resolve()
        ref = LABELS_DIR / f"{cid}.ref.txt"
        index.append(
            f"- [ ] `{cid}` lang={rec.get('prior_language')} ch={ch} "
            f"{rec.get('duration_sec')}s — audio: `{audio}` — ref: `{ref.name}`"
        )
        if ref.exists():
            kept += 1
            continue
        body = _TEMPLATE.format(
            cid=cid,
            lang=rec.get("prior_language"),
            ch=ch,
            dur=rec.get("duration_sec"),
            audio=audio,
        )
        if prefill:
            before = config.TRANSCRIPT_DIR / f"{cid}.before.json"
            text = json.loads(before.read_text(encoding="utf-8"))["full_text"]
            body += re.sub(r"\[[A-Za-z]+\]", "", text).strip() + "\n"
        ref.write_text(body, encoding="utf-8")
        created += 1

    (LABELS_DIR / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    print(  # noqa: T201 - spike CLI summary
        f"\nLabeling set: {len(chosen)} calls ({created} new templates, {kept} kept).\n"
        f"Fill in references under {LABELS_DIR}/<id>.ref.txt "
        f"(audio paths in INDEX.md), then run:\n"
        f"  python -m AtamuraOKK.spike.auphonic_ab wer\n"
    )
    logger.info("label-init: {n} calls -> {p}", n=len(chosen), p=LABELS_DIR)


@dataclass(slots=True)
class _Scored:
    id: int
    lang: str | None
    ch: int | None
    ref_words: int
    wer_before: float
    wer_after: float
    cer_before: float
    cer_after: float


def _score_one(cid: int, ref: str) -> _Scored | None:
    rec = json.loads(config.RESULTS.read_text(encoding="utf-8"))
    meta: dict[str, Any] = next((r for r in rec if r["id"] == cid), {})
    before = _normalize(
        json.loads((config.TRANSCRIPT_DIR / f"{cid}.before.json").read_text())[
            "full_text"
        ]
    )
    after = _normalize(
        json.loads((config.TRANSCRIPT_DIR / f"{cid}.after.json").read_text())[
            "full_text"
        ]
    )
    return _Scored(
        id=cid,
        lang=meta.get("prior_language"),
        ch=(meta.get("orig") or {}).get("channels"),
        ref_words=len(ref.split()),
        wer_before=jiwer.wer(ref, before),
        wer_after=jiwer.wer(ref, after),
        cer_before=cast("float", jiwer.cer(ref, before)),
        cer_after=cast("float", jiwer.cer(ref, after)),
    )


def compute() -> None:
    """Score every filled-in reference and report before vs after WER/CER."""
    scored: list[_Scored] = []
    for ref_file in sorted(LABELS_DIR.glob("*.ref.txt")):
        cid = int(ref_file.name.split(".")[0])
        ref = _ref_body(ref_file)
        if not ref:
            continue
        s = _score_one(cid, ref)
        if s:
            scored.append(s)

    if not scored:
        print(  # noqa: T201
            f"No filled-in references found under {LABELS_DIR}. "
            "Run `label-init` and transcribe the audio first."
        )
        return

    out = config.OUT_DIR / "wer.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "id",
                "lang",
                "ch",
                "ref_words",
                "wer_before",
                "wer_after",
                "wer_delta",
                "cer_before",
                "cer_after",
                "cer_delta",
            ]
        )
        for s in scored:
            w.writerow(
                [
                    s.id,
                    s.lang,
                    s.ch,
                    s.ref_words,
                    f"{s.wer_before:.4f}",
                    f"{s.wer_after:.4f}",
                    f"{s.wer_after - s.wer_before:+.4f}",
                    f"{s.cer_before:.4f}",
                    f"{s.cer_after:.4f}",
                    f"{s.cer_after - s.cer_before:+.4f}",
                ]
            )
    _report(scored, out)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _report(scored: list[_Scored], out: Path) -> None:
    n = len(scored)
    wb, wa = _mean([s.wer_before for s in scored]), _mean([s.wer_after for s in scored])
    cb, ca = _mean([s.cer_before for s in scored]), _mean([s.cer_after for s in scored])
    wer_helped = sum(s.wer_after < s.wer_before for s in scored)
    cer_helped = sum(s.cer_after < s.cer_before for s in scored)
    lines = [
        f"{'id':>6} {'lg':>2} {'ch':>2} {'refW':>5} "
        f"{'WERb':>6} {'WERa':>6} {'dWER':>7} {'CERb':>6} {'CERa':>6} {'dCER':>7}",
    ]
    for s in sorted(scored, key=lambda x: x.wer_after - x.wer_before):
        lines.append(
            f"{s.id:>6} {s.lang!s:>2} {s.ch!s:>2} {s.ref_words:>5} "
            f"{s.wer_before:>6.3f} {s.wer_after:>6.3f} "
            f"{s.wer_after - s.wer_before:>+7.3f} "
            f"{s.cer_before:>6.3f} {s.cer_after:>6.3f} "
            f"{s.cer_after - s.cer_before:>+7.3f}"
        )
    verdict = (
        "AFTER (cleanup) is better"
        if wa < wb
        else "BEFORE (original) is better"
        if wa > wb
        else "tie"
    )
    print("\n".join(lines))  # noqa: T201
    print(  # noqa: T201
        f"\n=== {n} labeled calls ===\n"
        f"mean WER: before={wb:.3f}  after={wa:.3f}  (after-before={wa - wb:+.3f}); "
        f"cleanup lowered WER on {wer_helped}/{n}\n"
        f"mean CER: before={cb:.3f}  after={ca:.3f}  (after-before={ca - cb:+.3f}); "
        f"cleanup lowered CER on {cer_helped}/{n}\n"
        f"VERDICT: {verdict} (lower error = better). Detail -> {out}"
    )
    logger.info("wer: scored {n} calls -> {p}", n=n, p=out)
