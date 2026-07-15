"""Deterministic check for the «Дубль…» close reasons: is the number really a dupe.

Unlike every other отказ-причина, «Дубль по этому проекту» / «Дубль по другим
проектам» is a claim about the CRM, not about the conversation: the manager says this
lead is a copy of one we already hold. A transcript can never settle that (the LLM
judge rightly answers ``not_determinable``), but Bitrix can — the client's number
either appears on another deal or it does not. So these two reasons bypass the judge
and are checked against the CRM instead, which also means they need **no transcript
and no LLM**: they audit fine while Anthropic credits are out.

Verdicts:

- ``contradicted`` — the number carries no other deal and no lead. The «дубль» was
  invented and a live lead is buried. This is the finding «Отказы не по делу» shows.
- ``supported`` — a duplicate really exists. The *subtype* («этому» vs «другим
  проектам») is resolved from the deals' projects and recorded in ``details``
  (``subtype_ok``), but a wrong subtype is CRM hygiene, not a buried lead, so it
  does not contradict the close.
- ``not_determinable`` — the deal has no contact or no number, so there is nothing
  to look up.

The project is *not* a Bitrix field — «Жилой комплекс» is empty on virtually every TM
deal — so it is read off the deal TITLE («Лиды FB | Aqsai Resort New», «Крыша …
ЖК Keruen»), which is why :data:`_PROJECT_ALIASES` carries both the Latin and the
Cyrillic spelling of every ЖК. Roughly a third of TM deals carry no project in the
title at all (входящий звонок, Instagram-лиды) — for those the subtype is left
unresolved (``subtype_ok=None``) rather than guessed.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from AtamuraOKK.settings import settings

if TYPE_CHECKING:
    from AtamuraOKK.bitrix import BitrixClient

CHECK_ID = "duplicate/v1"
_CONCURRENCY = 4

# Title token -> canonical project. Titles spell the ЖК either way («Keruen» /
# «Керуен», «Aqsai Resort New» / «Аксай Резорт»), and some campaigns run projects the
# «Жилой комплекс» enum never got (Amaia, UIA, Dion) — so this table, not the enum,
# is the vocabulary.
_PROJECT_ALIASES = {
    "keruen": "keruen",
    "керуен": "keruen",
    "aqsai": "aqsai",
    "аксай": "aqsai",
    "amaia": "amaia",
    "амая": "amaia",
    "aura": "aura",
    "аура": "aura",
    "atmosfera": "atmosfera",
    "атмосфера": "atmosfera",
    "uia": "uia",
    "bravo": "bravo",
    "браво": "bravo",
    "dion": "dion",
    "дион": "dion",
    "arlan": "arlan",
    "арлан": "arlan",
    "ayala": "ayala",
    "аяла": "ayala",
    "atakent": "atakent",
    "атакент": "atakent",
    "neo": "neo",
    "нео": "neo",
    "monarch": "monarch",
    "монарх": "monarch",
    "discovery": "discovery",
    "дискавери": "discovery",
    "dulati": "dulati",
    "дулати": "dulati",
    "olympic": "olympic",
    "олимпик": "olympic",
}
_PROJECT_RE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE), canon)
    for token, canon in _PROJECT_ALIASES.items()
]


def project_of(title: str | None) -> str | None:
    """Canonical project (ЖК) named in a deal title, or None if it names none."""
    text = title or ""
    for pattern, canon in _PROJECT_RE:
        if pattern.search(text):
            return canon
    return None


def dup_reason_ids() -> dict[str, str]:
    """``{enum id: "same"|"other"}`` for the two «Дубль…» reasons (empty if unset)."""
    out: dict[str, str] = {}
    if settings.audit_dup_same_project_reason_id:
        out[settings.audit_dup_same_project_reason_id] = "same"
    if settings.audit_dup_other_project_reason_id:
        out[settings.audit_dup_other_project_reason_id] = "other"
    return out


@dataclass
class DuplicateCheck:
    """One deal's duplicate verdict, shaped like a judge verdict so it upserts alike."""

    verdict: str
    confidence: float
    justification: str
    evidence_quote: str
    details: dict[str, Any]

    def as_verdict(self) -> dict[str, Any]:
        """The judge-shaped dict ``_upsert_verdict`` persists."""
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "justification": self.justification,
            "evidence_quote": self.evidence_quote,
        }


async def contact_phones(bx: BitrixClient, ids: set[int]) -> dict[int, list[str]]:
    """``{contact id: [phone, …]}`` for a batch of contacts (one paged list call)."""
    if not ids:
        return {}
    out: dict[int, list[str]] = {}
    async for c in bx.list(
        "crm.contact.list",
        {"filter": {"ID": sorted(ids)}, "select": ["ID", "PHONE"]},
    ):
        phones = [str(p["VALUE"]) for p in (c.get("PHONE") or []) if p.get("VALUE")]
        out[int(c["ID"])] = phones
    return out


async def _duplicate_contact_ids(
    bx: BitrixClient, phones: list[str], own_contact_id: int
) -> list[int]:
    """Contacts sharing any of these numbers, per Bitrix's own dedupe index."""
    found = await bx.call(
        "crm.duplicate.findbycomm",
        {"entity_type": "CONTACT", "type": "PHONE", "values": phones},
    )
    ids = {int(i) for i in (found or {}).get("CONTACT") or []}
    ids.add(own_contact_id)
    return sorted(ids)


async def _lead_ids(bx: BitrixClient, phones: list[str]) -> list[int]:
    """Leads sharing any of these numbers (only consulted before we accuse)."""
    found = await bx.call(
        "crm.duplicate.findbycomm",
        {"entity_type": "LEAD", "type": "PHONE", "values": phones},
    )
    return sorted(int(i) for i in (found or {}).get("LEAD") or [])


async def _deals_of_contacts(
    bx: BitrixClient, contact_ids: list[int], exclude_deal_id: int
) -> list[dict[str, Any]]:
    """Every other deal hanging off these contacts (any funnel), oldest first."""
    deals: list[dict[str, Any]] = []
    async for d in bx.list(
        "crm.deal.list",
        {
            "filter": {"CONTACT_ID": contact_ids},
            "select": ["ID", "TITLE", "CATEGORY_ID", "STAGE_ID", "DATE_CREATE"],
            "order": {"DATE_CREATE": "ASC"},
        },
        max_items=settings.audit_dup_max_deals,
    ):
        if int(d["ID"]) != exclude_deal_id:
            deals.append(d)
    return deals


def _subtype(
    deal_title: str | None,
    tm_duplicates: list[dict[str, Any]],
    reason_kind: str,
) -> tuple[bool | None, str | None, list[str]]:
    """``(subtype_ok, expected_kind, duplicate projects)`` — None when unresolvable."""
    mine = project_of(deal_title)
    theirs = sorted({p for d in tm_duplicates if (p := project_of(d.get("TITLE")))})
    if mine is None or not theirs:
        return None, None, theirs
    expected = "same" if mine in theirs else "other"
    return expected == reason_kind, expected, theirs


def _describe(deals: list[dict[str, Any]], limit: int = 3) -> str:
    shown = ", ".join(f"#{d['ID']} «{d.get('TITLE') or '—'}»" for d in deals[:limit])
    extra = f" и ещё {len(deals) - limit}" if len(deals) > limit else ""
    return shown + extra


async def check_one(
    bx: BitrixClient,
    *,
    deal: dict[str, Any],
    reason_kind: str,
    phones: list[str],
    sem: asyncio.Semaphore | None = None,
) -> DuplicateCheck:
    """Check one «Дубль…» deal against the CRM.

    Never raises: a Bitrix failure degrades to ``verdict="error"`` (as ``judge_one``
    does) so one bad lookup cannot abort the batch, and the cursor retries it.
    """
    deal_id = int(deal["ID"])
    raw_contact = deal.get("CONTACT_ID")
    contact_id = (
        int(raw_contact)
        if raw_contact is not None and str(raw_contact) not in ("", "0")
        else None
    )
    details: dict[str, Any] = {"check": CHECK_ID, "phones": phones}
    if contact_id is None or not phones:
        return DuplicateCheck(
            verdict="not_determinable",
            confidence=0.0,
            justification=(
                "У сделки нет контакта или номера телефона — проверить дубль по CRM "
                "невозможно."
            ),
            evidence_quote="",
            details=details,
        )
    try:
        async with _MaybeSemaphore(sem):
            dup_contacts = await _duplicate_contact_ids(bx, phones, contact_id)
            others = await _deals_of_contacts(bx, dup_contacts, deal_id)
            leads = await _lead_ids(bx, phones) if not others else []
    except Exception as exc:  # record, don't abort the batch (mirrors judge_one)
        logger.warning("audit dup-check failed for deal {d}: {e}", d=deal_id, e=exc)
        return DuplicateCheck(
            verdict="error",
            confidence=0.0,
            justification=f"{type(exc).__name__}: {exc}",
            evidence_quote="",
            details=details,
        )

    details["duplicate_contact_ids"] = dup_contacts
    details["duplicate_deal_ids"] = [int(d["ID"]) for d in others]
    details["lead_ids"] = leads
    phone = phones[0]

    # Nothing anywhere carries this number: the «дубль» is invented — a live lead
    # was closed on a reason that does not exist. This is the finding worth a nudge.
    if not others and not leads:
        return DuplicateCheck(
            verdict="contradicted",
            confidence=1.0,
            justification=(
                f"Сделка закрыта как «дубль», но дубля нет: номер {phone} не найден "
                f"ни в одной другой сделке или лиде CRM."
            ),
            evidence_quote="",
            details=details,
        )
    # A lead (no deal) carries the number — a real duplicate record, so the close
    # stands; we just can't speak to the project.
    if not others:
        return DuplicateCheck(
            verdict="supported",
            confidence=1.0,
            justification=(
                f"Дубль подтверждён: номер {phone} найден в лиде(ах) "
                f"{', '.join(f'#{i}' for i in leads)}; другой сделки нет."
            ),
            evidence_quote="",
            details=details,
        )

    tm_others = [
        d
        for d in others
        if str(d.get("CATEGORY_ID")) == str(settings.companion_tm_category_id)
    ]
    details["tm_duplicate_deal_ids"] = [int(d["ID"]) for d in tm_others]
    if not tm_others:
        return DuplicateCheck(
            verdict="supported",
            confidence=1.0,
            justification=(
                f"Дубль подтверждён, но вне воронки ТМ: номер {phone} уже ведётся в "
                f"сделке {_describe(others)} (другая воронка)."
            ),
            evidence_quote="",
            details=details,
        )

    subtype_ok, expected, projects = _subtype(deal.get("TITLE"), tm_others, reason_kind)
    details["project"] = project_of(deal.get("TITLE"))
    details["duplicate_projects"] = projects
    details["subtype_ok"] = subtype_ok
    details["expected_reason_kind"] = expected
    base = f"Дубль подтверждён: номер {phone} уже есть в сделке {_describe(tm_others)}."
    if subtype_ok is False:
        should = (
            "«Дубль по этому проекту»"
            if expected == "same"
            else "«Дубль по другим проектам»"
        )
        base += (
            f" Но подтип указан неверно: дубль по проекту {', '.join(projects)} — "
            f"следовало выбрать {should}."
        )
    return DuplicateCheck(
        verdict="supported",
        confidence=1.0,
        justification=base,
        evidence_quote="",
        details=details,
    )


async def check_many(
    bx: BitrixClient, targets: list[dict[str, Any]]
) -> list[DuplicateCheck]:
    """Check a batch of «Дубль…» deals concurrently (phones resolved in one call)."""
    if not targets:
        return []
    contact_ids = {
        int(t["deal"]["CONTACT_ID"])
        for t in targets
        if t["deal"].get("CONTACT_ID") not in (None, "", 0, "0")
    }
    phones = await contact_phones(bx, contact_ids)
    sem = asyncio.Semaphore(_CONCURRENCY)
    return list(
        await asyncio.gather(
            *(
                check_one(
                    bx,
                    deal=t["deal"],
                    reason_kind=t["reason_kind"],
                    phones=phones.get(int(t["deal"].get("CONTACT_ID") or 0), []),
                    sem=sem,
                )
                for t in targets
            )
        )
    )


class _MaybeSemaphore:
    """Async-with over an optional semaphore (no-op when None)."""

    def __init__(self, sem: asyncio.Semaphore | None) -> None:
        self._sem = sem

    async def __aenter__(self) -> None:
        if self._sem is not None:
            await self._sem.acquire()

    async def __aexit__(self, *exc: object) -> None:
        if self._sem is not None:
            self._sem.release()
