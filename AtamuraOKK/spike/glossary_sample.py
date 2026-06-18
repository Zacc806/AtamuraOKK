"""Sample-first validation for the ЖК/address glossary correction.

Before enabling ``glossary_correct_enabled`` in production, run this over a batch
of already-transcribed meetings (real Yandex output) to see exactly how Yandex
renders the complex names and Kazakh toponyms, and how the LLM corrector repairs
them. It is read-only: it pulls existing transcripts, runs the corrector, and
writes a before→after record per recording for review — it never mutates state.

Tune ``AtamuraOKK/glossary/canonical.py`` and the prompt in ``llm_correct.py``
until the named entities come out right, then flip the flag on.

    python -m AtamuraOKK.spike glossary-sample --limit 20
"""

from __future__ import annotations

import asyncio
import json

from loguru import logger

from AtamuraOKK.glossary.llm_correct import EntityCorrector
from AtamuraOKK.scoring.meetings.config import config
from AtamuraOKK.scoring.meetings.store import MeetingStatus, MeetingStore
from AtamuraOKK.settings import settings

# Statuses whose rows carry a finished transcript (real Yandex output).
_WITH_TRANSCRIPT = (MeetingStatus.TRANSCRIBED, MeetingStatus.SCORED)


def _gather_rows(store: MeetingStore, limit: int) -> list[tuple[int, str]]:
    """Return up to ``limit`` (file_id, transcript) pairs from transcribed rows."""
    pairs: list[tuple[int, str]] = []
    for status in _WITH_TRANSCRIPT:
        for row in store.claim(status, limit):
            transcript = row["transcript"]
            if transcript and transcript.strip():
                pairs.append((int(row["file_id"]), transcript))
            if len(pairs) >= limit:
                return pairs
    return pairs


async def _run(limit: int) -> None:
    store = MeetingStore()
    try:
        rows = _gather_rows(store, limit)
    finally:
        store.close()

    if not rows:
        logger.warning("No transcribed meetings found to sample.")
        return

    # Force the corrector on regardless of the production flag — that's the point
    # of the sample. Model defaults to glossary_correct_model.
    corrector = EntityCorrector(
        api_key=config.anthropic_api_key,
        model=config.glossary_correct_model,
    )

    out_path = settings.spike_dir / "glossary_sample.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    changed = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for idx, (file_id, raw) in enumerate(rows, start=1):
            corrected = await corrector.correct(raw)
            was_changed = corrected != raw
            changed += int(was_changed)
            fh.write(
                json.dumps(
                    {
                        "file_id": file_id,
                        "changed": was_changed,
                        "raw": raw,
                        "corrected": corrected,
                    },
                    ensure_ascii=False,
                )
                + "\n",
            )
            logger.info(
                "[{i}/{n}] meeting {id}: {state}",
                i=idx,
                n=len(rows),
                id=file_id,
                state="CHANGED" if was_changed else "unchanged",
            )

    logger.info(
        "Sampled {n} transcripts ({c} changed) -> {p}",
        n=len(rows),
        c=changed,
        p=out_path,
    )


def run_sample(limit: int = 20) -> None:
    """Transcribe-free sample: correct existing meeting transcripts and dump diffs."""
    asyncio.run(_run(limit))
