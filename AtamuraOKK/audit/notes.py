"""The manager's own notes on the deal card — extra evidence for the LLM judge.

A close reason is often documented *outside* the call: the manager writes it into the
deal's timeline as a note («Одобрение по Алматы жастары до 20 миллионов, у нас нет
вариантов, по срокам не подходит») while the recorded conversation never names it.
Judged on the transcript alone such a deal reads as ``contradicted`` when in fact the
reason is stated and true — a false flag landing in «Отказы не по делу». So the judge
(and *only* the judge — see below) gets the card notes alongside the transcript.

Notes are read with ``crm.timeline.comment.list``, one command per deal packed
``PAGE_SIZE`` to a Bitrix ``batch`` — the same shape ``web/api/v1/hygiene.py`` uses
for the «примечание по шаблону» criterion, as Bitrix has no cross-entity comment filter.
Measured on this portal: ~95% of flagged deals carry a note, median 3 notes / 127 chars,
so the whole addition is a couple of batch reads per pass and ~40 extra prompt tokens
per deal.

**55% of raw timeline comments are Wazzup/WhatsApp integration traffic** — outbound
templates, auto-greetings, delivery errors — which is machine output, not the manager's
stated reason, and would drown the real notes in the prompt. Those are dropped
(``_INTEGRATION_HOSTS``), along with BB-code markup, image/link blocks and anything too
short to be a sentence.

The rule routes (``audit/duplicates.py``, ``audit/telephony.py``) deliberately do
**not** consult notes: they settle a claim about the CRM or about telephony against a
hard fact (the number demonstrably answered; the duplicate demonstrably does or does
not exist), and «ндз» typed three times is not evidence against a Voximplant record.
Their value is being un-arguable, so notes must not soften them.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from loguru import logger

from AtamuraOKK.bitrix.client import PAGE_SIZE
from AtamuraOKK.settings import settings

if TYPE_CHECKING:
    from AtamuraOKK.bitrix import BitrixClient

_CONCURRENCY = 4
# Media/link blocks go whole, then any remaining BB-code tag (mirrors hygiene.py).
_BB_MEDIA = re.compile(r"\[(img|url)[^\]]*\].*?\[/\1\]", re.IGNORECASE | re.DOTALL)
_BB_TAG = re.compile(r"\[/?[^\]]{1,80}\]")
_BARE_URL = re.compile(r"https?://\S+")
_WS = re.compile(r"\s+")
# Hosts whose comments are posted by an integration, not typed by a human. The Wazzup
# channel stamps every message with its own avatar/image URL — that is what marks it.
_INTEGRATION_HOSTS = ("wazzup24.com",)


def _is_integration(raw: str) -> bool:
    """Whether this comment was posted by a messenger integration, not a person."""
    low = raw.lower()
    return any(host in low for host in _INTEGRATION_HOSTS)


def clean_note(raw: str) -> str:
    """One timeline comment as plain text, or ``""`` if nothing human is left."""
    text = _BB_MEDIA.sub(" ", raw or "")
    text = _BB_TAG.sub(" ", text)
    text = _BARE_URL.sub(" ", text).replace("&nbsp;", " ")
    text = _WS.sub(" ", text).strip()
    return text if len(text) >= settings.audit_note_min_chars else ""


def _select(comments: list[dict[str, Any]]) -> list[str]:
    """The human notes of one deal, oldest first, newest kept when the cap bites.

    The most recent notes are the ones written around the close, so when a card has more
    notes than the cap allows it is the *oldest* that are dropped — but the surviving
    ones are still shown to the judge in chronological order, which is how they read.
    """
    notes: list[str] = []
    for c in comments:
        raw = str(c.get("COMMENT") or "")
        if _is_integration(raw):
            continue
        text = clean_note(raw)
        if text:
            notes.append(text)
    if settings.audit_note_max_count > 0:
        notes = notes[-settings.audit_note_max_count :]
    return notes


def format_notes(notes: list[str]) -> str:
    """The notes block for the judge prompt (``""`` when there is nothing to show)."""
    if not notes:
        return ""
    cap = settings.audit_note_max_chars
    lines: list[str] = []
    used = 0
    for note in notes:
        if cap > 0 and used + len(note) > cap:
            break
        lines.append(f"- {note}")
        used += len(note)
    return "\n".join(lines)


async def notes_for_deals(
    bx: BitrixClient,
    deal_ids: list[int],
) -> dict[int, list[str]]:
    """``{deal id: [note, …]}`` for these deals (missing/failed deals map to ``[]``).

    Never raises: notes are *supplementary* evidence, so a Bitrix failure degrades to
    "no notes" and the judge still runs on the transcript alone. A batch already reports
    per-command failures as absent keys rather than raising.
    """
    out: dict[int, list[str]] = {d: [] for d in deal_ids}
    if not deal_ids or not settings.audit_use_card_notes:
        return out
    chunks = [deal_ids[i : i + PAGE_SIZE] for i in range(0, len(deal_ids), PAGE_SIZE)]
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def fetch(chunk: list[int]) -> dict[str, Any]:
        async with gate:
            return await bx.batch(
                {
                    str(deal_id): (
                        "crm.timeline.comment.list",
                        {
                            "filter[ENTITY_ID]": deal_id,
                            "filter[ENTITY_TYPE]": "deal",
                            "select[]": ["ID", "COMMENT", "AUTHOR_ID", "CREATED"],
                        },
                    )
                    for deal_id in chunk
                },
            )

    try:
        results = await asyncio.gather(*(fetch(c) for c in chunks))
    except Exception as exc:  # supplementary evidence — never fail the pass over it
        logger.warning(
            "audit: card notes unavailable, judging without them: {e}", e=exc
        )
        return out

    for res in results:
        for key, rows in res.items():
            try:
                deal_id = int(key)
            except ValueError:
                continue
            if isinstance(rows, list):
                out[deal_id] = _select(rows)
    return out
