"""Deterministic check for «недозвон»-family reasons: was the number ever answered.

Unlike a substantive отказ («не подходит локация», «дорого»), «Хронический
недозвон» / «Автодозвон» / «Перестал выходить на связь» is a claim about
*telephony*, not about a conversation: the manager says this lead was never reached.
There is no answered call to transcribe — precisely why the LLM judge is useless here
(it is told these are «почти всегда not_determinable», ``audit/judge.py``). But
Voximplant can settle it: the client's number either was answered at least once in the
deal's lifetime or it was not. So these reasons bypass the judge and are checked against
``voximplant.statistic.get`` — which, unlike our ``calls`` table, *does* carry
unanswered attempts — needing **no transcript and no LLM** (they audit with no credits).

Verdicts (flag rule = «answered at all»):

- ``contradicted`` — at least one *answered* call (``CALL_FAILED_CODE ==
  ingest_success_code``) to the client's number sits inside the window. The «недозвон»
  is false: a live lead the manager actually reached was closed as never-reached. This
  is the finding «Отказы не по делу» shows.
- ``supported`` — only unanswered attempts (or no telephony rows at all): close stands.
- ``not_determinable`` — the deal has no contact or no phone number, nothing to look up.

Two facts pinned down by a live probe against this portal:

* The ``PHONE_NUMBER`` FILTER key **is** honored by ``voximplant.statistic.get`` — a
  single targeted, date-bounded pull per phone is enough; no need to page a whole
  manager's history.
* The ``CRM_ENTITY_*`` fields on statistic rows do **not** reliably link back to the
  deal's contact for these leads, so matching is by the number, not by CRM id.

The window is ``[DATE_CREATE - 1d, CLOSEDATE + 1d]``, but Bitrix ``CLOSEDATE`` is
unreliable (it can precede ``DATE_CREATE`` — a planned-close default), so when it does
not sit after creation the window falls back to ``DATE_CREATE + _MAX_WINDOW_DAYS`` to
still bracket the dial attempts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger

from AtamuraOKK.settings import settings

if TYPE_CHECKING:
    from AtamuraOKK.bitrix import BitrixClient

CHECK_ID = "never_reached/v1"
_CONCURRENCY = 4
_CREATE_BUFFER = timedelta(days=1)  # dials logged just before the lead was created
_CLOSE_BUFFER = timedelta(days=1)  # a close logged shortly after the last dial
_MAX_WINDOW_DAYS = timedelta(days=30)  # fallback span when CLOSEDATE is unusable
_MAX_ROWS = 500  # safety cap on a single phone's paged pull


def never_reached_reason_ids() -> set[str]:
    """The enum ids routed to the telephony check (empty if the setting is unset)."""
    return {r for r in settings.audit_never_reached_reason_ids if r}


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def audit_window(deal: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """``[start, end]`` bracketing the deal's dials, or None without a create date.

    ``CLOSEDATE`` is used only when it actually sits after ``DATE_CREATE``; otherwise it
    is a planned-close default and the window falls back to a bounded span past create.
    """
    created = _parse_dt(deal.get("DATE_CREATE"))
    if not created:
        return None
    start = created - _CREATE_BUFFER
    closed = _parse_dt(deal.get("CLOSEDATE"))
    if closed and closed > created:
        end = closed + _CLOSE_BUFFER
    else:
        end = created + _MAX_WINDOW_DAYS
    return start, end


@dataclass
class TelephonyCheck:
    """One deal's never-reached verdict, judge-shaped so it upserts like the rest."""

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


async def _attempts_for_phone(
    bx: BitrixClient, phone: str, window: tuple[datetime, datetime]
) -> list[dict[str, Any]]:
    """Every Voximplant row for one number inside the window (one targeted pull)."""
    start, end = window
    rows: list[dict[str, Any]] = []
    async for r in bx.list(
        "voximplant.statistic.get",
        {
            "FILTER": {
                "PHONE_NUMBER": phone,
                ">=CALL_START_DATE": start.isoformat(),
                "<CALL_START_DATE": end.isoformat(),
            },
            "ORDER": {"CALL_START_DATE": "ASC"},
        },
        max_items=_MAX_ROWS,
    ):
        rows.append(r)
    return rows


async def check_one(
    bx: BitrixClient,
    *,
    deal: dict[str, Any],
    phones: list[str],
    sem: asyncio.Semaphore | None = None,
) -> TelephonyCheck:
    """Check one «недозвон» deal against Voximplant.

    Never raises: a Bitrix failure degrades to ``verdict="error"`` (as ``judge_one`` and
    ``duplicates.check_one`` do) so one bad lookup cannot abort the batch; the cursor
    retries it.
    """
    deal_id = int(deal["ID"])
    details: dict[str, Any] = {"check": CHECK_ID, "phones": phones}
    window = audit_window(deal)
    if not phones or window is None:
        return TelephonyCheck(
            verdict="not_determinable",
            confidence=0.0,
            justification=(
                "У сделки нет контакта, номера телефона или даты создания — проверить "
                "недозвон по звонкам невозможно."
            ),
            evidence_quote="",
            details=details,
        )

    details["window"] = [window[0].isoformat(), window[1].isoformat()]
    try:
        async with _MaybeSemaphore(sem):
            rows: list[dict[str, Any]] = []
            for phone in phones:
                rows.extend(await _attempts_for_phone(bx, phone, window))
    except Exception as exc:  # record, don't abort the batch (mirrors judge_one)
        logger.warning(
            "audit telephony-check failed for deal {d}: {e}", d=deal_id, e=exc
        )
        return TelephonyCheck(
            verdict="error",
            confidence=0.0,
            justification=f"{type(exc).__name__}: {exc}",
            evidence_quote="",
            details=details,
        )

    success = settings.ingest_success_code
    answered = [r for r in rows if str(r.get("CALL_FAILED_CODE")) == success]
    codes_seen: dict[str, int] = {}
    for r in rows:
        code = str(r.get("CALL_FAILED_CODE"))
        codes_seen[code] = codes_seen.get(code, 0) + 1
    details["attempts"] = len(rows)
    details["answered"] = len(answered)
    details["unanswered"] = len(rows) - len(answered)
    details["codes_seen"] = codes_seen
    details["answered_call_ids"] = [
        str(r.get("CALL_ID")) for r in answered if r.get("CALL_ID")
    ]

    # At least one answered call: the «недозвон» is false — a lead that was actually
    # reached got closed as never-reached. This is the finding worth a nudge.
    if answered:
        when = answered[0].get("CALL_START_DATE") or ""
        return TelephonyCheck(
            verdict="contradicted",
            confidence=1.0,
            justification=(
                f"Закрыто как недозвон, но дозвон был: номер клиента отвечал "
                f"{len(answered)} раз(а) (первый — {when}). Живой лид закрыт как "
                f"недозвон."
            ),
            evidence_quote="",
            details=details,
        )
    return TelephonyCheck(
        verdict="supported",
        confidence=1.0,
        justification=(
            f"Недозвон подтверждён: за период сделки было {len(rows)} попыток "
            f"дозвона, ни одна не отвечена."
        ),
        evidence_quote="",
        details=details,
    )


async def check_many(
    bx: BitrixClient, targets: list[dict[str, Any]]
) -> list[TelephonyCheck]:
    """Check a batch of «недозвон» deals concurrently (phones resolved in one call)."""
    if not targets:
        return []
    from AtamuraOKK.audit.duplicates import contact_phones  # noqa: PLC0415

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
