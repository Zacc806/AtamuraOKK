"""Stage 4: compute Word Error Rate per language.

Inputs the operator provides after transcription:
  - ``<spike_dir>/refs/<call_id>.txt`` — hand-corrected reference transcript.
  - ``<spike_dir>/refs/labels.json`` — optional ``{call_id: "ru"|"kk"}`` map of
    the *true* language. Missing entries fall back to Whisper's detected
    language (and are flagged, since auto-detect itself is under test).

Prints a per-language WER table — the headline number for the decision gate.
``jiwer`` ships in the ``spike`` dependency group.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass

from loguru import logger

from AtamuraOKK.settings import settings

_SPEAKER_TAG = re.compile(r"^\[(agent|customer|unknown)\]\s*", re.MULTILINE)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, strip speaker tags + punctuation, collapse whitespace.

    WER for LLM-scoring purposes ignores casing/punctuation; we care about
    word identity, not verbatim formatting.
    """
    text = _SPEAKER_TAG.sub("", text)
    text = text.lower()
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


@dataclass
class CallWER:
    """WER result for a single call."""

    call_id: str
    language: str
    wer: float
    ref_words: int


def _load_labels() -> dict[str, str]:
    path = settings.spike_dir / "refs" / "labels.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def compute() -> list[CallWER]:
    """Compute WER for every call that has both a transcript and a reference."""
    from jiwer import wer as jiwer_wer  # noqa: PLC0415

    refs_dir = settings.spike_dir / "refs"
    trans_dir = settings.spike_dir / "transcripts"
    if not refs_dir.exists():
        raise FileNotFoundError(
            f"{refs_dir} not found — create hand-corrected <call_id>.txt files "
            "there first (see docs/transcription-eval.md).",
        )
    labels = _load_labels()

    results: list[CallWER] = []
    for trans_file in sorted(trans_dir.glob("*.json")):
        ref_file = refs_dir / f"{trans_file.stem}.txt"
        if not ref_file.exists():
            continue
        payload = json.loads(trans_file.read_text(encoding="utf-8"))
        call_id = payload["call_id"]
        ref = normalize(ref_file.read_text(encoding="utf-8"))
        hyp = normalize(payload.get("full_text", ""))
        if not ref:
            continue
        score = jiwer_wer(ref, hyp)
        language = (
            labels.get(call_id)
            or labels.get(trans_file.stem)
            or (payload.get("language", "auto") + "?")
        )
        results.append(
            CallWER(
                call_id=call_id,
                language=language,
                wer=round(float(score), 4),
                ref_words=len(ref.split()),
            ),
        )
    return results


def report(results: list[CallWER]) -> None:
    """Print a per-call and per-language WER summary."""
    if not results:
        logger.warning("No (transcript, reference) pairs found — nothing to score.")
        return

    by_lang: dict[str, list[CallWER]] = {}
    for r in results:
        by_lang.setdefault(r.language, []).append(r)

    logger.info("Per-call WER:")
    for r in sorted(results, key=lambda x: x.language):
        logger.info(
            "  {id:<48} {lang:<6} WER={wer:6.1%}  ({n} ref words)",
            id=r.call_id,
            lang=r.language,
            wer=r.wer,
            n=r.ref_words,
        )

    logger.info("Per-language WER (word-weighted mean):")
    for lang, items in sorted(by_lang.items()):
        total_words = sum(i.ref_words for i in items)
        weighted = (
            sum(i.wer * i.ref_words for i in items) / total_words
            if total_words
            else statistics.fmean(i.wer for i in items)
        )
        logger.info(
            "  {lang:<6} n={n:<3} mean WER={wer:6.1%}  ({words} ref words)",
            lang=lang,
            n=len(items),
            wer=weighted,
            words=total_words,
        )
