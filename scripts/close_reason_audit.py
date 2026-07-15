"""One-off audit: does a lead's stated close reason match the actual call?

For every closed-lost deal (Bitrix ``STAGE_SEMANTIC_ID='F'`` in the telemarketing
category) a manager picks a close reason from the «Причина закрытия/отказа» dropdown
(deal enum field ``settings.companion_closed_reason_field``). This script joins that
stated reason against the *actual* recorded call(s) we hold a transcript for, so a
human — or, with ``--judge``, Claude — can check whether the reason is truthful.

Two stages, both read-only (no writes to Bitrix or our DB, no schema change):

  Stage 1 (always): pull transcripts from Postgres, resolve each client's deals via
    Bitrix, keep the closed-lost ones, label the reason, and write a review CSV
    (stated reason next to the full transcript). Works with no LLM.

  Stage 2 (``--judge``, needs Anthropic credits): send transcript + stated reason to
    Claude via forced tool-use; it returns supported / contradicted / not_determinable
    with a justification and an evidence quote. Writes a verdicts CSV + a summary with
    the per-manager mismatch rate.

Resolution reuses the Contact→deals pattern from ``scripts/outcome_correlation.py``;
the closed-lost filter and enum-label logic mirror ``web/api/v1/analytics.py``. Run:

    uv run python -m scripts.close_reason_audit --limit 50            # Stage 1 only
    uv run python -m scripts.close_reason_audit --limit 50 --judge    # + LLM verdicts

Coverage caveat: we only hold transcripts for the *analyzable* subset (calls until the
client qualifies). Never-qualified clients stay fully in scope, so closed-lost,
never-qualified leads are where call history is fullest. Some reasons (e.g.
«Хронический недозвон») are not verifiable from an answered-call transcript — the judge
returns ``not_determinable`` for those rather than forcing a verdict. The «Дубль…»
reasons are no longer judged at all: the standing audit settles them against the CRM
instead (``AtamuraOKK/audit/duplicates.py``), which this probe does not replicate.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import text

from AtamuraOKK.audit.judge import VERDICTS as _VERDICTS
from AtamuraOKK.audit.judge import build_judge_client, judge_one
from AtamuraOKK.audit.service import reason_enum_labels as _reason_enum_labels
from AtamuraOKK.audit.service import reason_ids as _reason_ids
from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.settings import settings

_CONCURRENCY = 6
_UNSPECIFIED_REASON = "Не указана"  # mirrors web/api/v1/analytics.py
_REASON_FIELD = settings.companion_closed_reason_field
_CATEGORY_ID = settings.companion_tm_category_id

# Newest N distinct clients that have a transcript on file.
CLIENTS_SQL = text(
    """
    SELECT c.client_key AS client_key, MAX(c.started_at) AS last_at
    FROM transcripts t
    JOIN calls c ON c.id = t.call_id
    WHERE c.client_key IS NOT NULL AND c.client_key <> ''
    GROUP BY c.client_key
    ORDER BY MAX(c.started_at) DESC
    LIMIT :limit
    """
)

# All transcripts for the selected clients, oldest-first within a client.
TRANSCRIPTS_SQL = text(
    """
    SELECT c.id           AS call_id,
           c.client_key   AS client_key,
           c.manager_id   AS manager_id,
           c.started_at   AS started_at,
           t.full_text    AS full_text
    FROM transcripts t
    JOIN calls c ON c.id = t.call_id
    WHERE c.client_key = ANY(:client_keys)
    ORDER BY c.client_key, c.started_at
    """
)


@dataclass
class ClientCalls:
    """A client's transcribed calls, keyed by ``client_key``."""

    client_key: str
    manager_id: int | None = None  # from the most recent call
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def transcript(self) -> str:
        """All calls concatenated, each labeled with its id + timestamp."""
        parts = []
        for c in self.calls:
            head = f"=== звонок {c['call_id']} @ {c['started_at']} ==="
            parts.append(f"{head}\n{c['full_text']}")
        return "\n\n".join(parts)

    @property
    def call_ids(self) -> list[int]:
        return [c["call_id"] for c in self.calls]

    @property
    def started_range(self) -> str:
        if not self.calls:
            return ""
        return f"{self.calls[0]['started_at']} .. {self.calls[-1]['started_at']}"


@dataclass
class AuditRow:
    """One closed-lost deal joined to its client's transcript."""

    deal_id: str
    assigned_by_id: str | None
    manager_id: int | None
    client_key: str
    close_reason: str
    reason_id: str | None
    n_calls: int
    call_ids: list[int]
    started_range: str
    transcript: str


async def _closed_lost_deals(
    bx: BitrixClient, entity_type: str, entity_id: str, sem: asyncio.Semaphore
) -> list[dict[str, Any]]:
    """The client's closed-lost deals (``STAGE_SEMANTIC_ID='F'``) in the TM category.

    Returns the raw deal dicts (ID, ASSIGNED_BY_ID, reason field). Empty for
    LEAD/PHONE-only clients that don't resolve to deals.
    """
    if entity_type == "CONTACT":
        filter_: dict[str, Any] = {"CONTACT_ID": entity_id}
    elif entity_type == "COMPANY":
        filter_ = {"COMPANY_ID": entity_id}
    elif entity_type == "DEAL":
        filter_ = {"ID": entity_id}
    else:  # LEAD / PHONE-only — not resolvable to a deal
        return []

    filter_.update({"CATEGORY_ID": _CATEGORY_ID, "STAGE_SEMANTIC_ID": "F"})
    select = ["ID", "ASSIGNED_BY_ID", "STAGE_ID", _REASON_FIELD]
    out: list[dict[str, Any]] = []
    async with sem:
        try:
            async for d in bx.list(
                "crm.deal.list",
                {"filter": filter_, "select": select, "order": {"ID": "DESC"}},
            ):
                out.append(d)
        except BitrixError as exc:
            logger.warning("Deal lookup failed for {t}:{i}: {e}", t=entity_type, i=entity_id, e=exc)
    return out


async def _load_clients(limit: int) -> dict[str, ClientCalls]:
    """Stage-1 DB pull: newest ``limit`` clients with transcripts, grouped."""
    async with session_scope() as session:
        keys = [r["client_key"] for r in (await session.execute(CLIENTS_SQL, {"limit": limit})).mappings()]
        if not keys:
            return {}
        rows = (await session.execute(TRANSCRIPTS_SQL, {"client_keys": keys})).mappings().all()

    clients: dict[str, ClientCalls] = {k: ClientCalls(client_key=k) for k in keys}
    for r in rows:
        cc = clients[r["client_key"]]
        cc.calls.append(dict(r))
        cc.manager_id = r["manager_id"]  # rows are oldest-first, so this ends on the latest
    logger.info("Loaded {c} clients / {n} transcribed calls", c=len(clients), n=len(rows))
    return clients


async def build_audit_rows(limit: int) -> list[AuditRow]:
    """Stage 1: join transcribed clients to their closed-lost deals + reasons."""
    clients = await _load_clients(limit)
    if not clients:
        return []

    sem = asyncio.Semaphore(_CONCURRENCY)
    async with BitrixClient() as bx:
        labels = await _reason_enum_labels(bx, _REASON_FIELD)

        async def resolve(cc: ClientCalls) -> list[dict[str, Any]]:
            entity_type, _, entity_id = cc.client_key.partition(":")
            return await _closed_lost_deals(bx, entity_type, entity_id, sem)

        deals_per_client = await asyncio.gather(*(resolve(cc) for cc in clients.values()))

    rows: list[AuditRow] = []
    for cc, deals in zip(clients.values(), deals_per_client, strict=True):
        for d in deals:
            ids = _reason_ids(d.get(_REASON_FIELD))
            reason_id = ids[0] if ids else None
            rows.append(
                AuditRow(
                    deal_id=str(d.get("ID")),
                    assigned_by_id=str(d.get("ASSIGNED_BY_ID")) if d.get("ASSIGNED_BY_ID") else None,
                    manager_id=cc.manager_id,
                    client_key=cc.client_key,
                    close_reason=labels.get(reason_id, reason_id) if reason_id else _UNSPECIFIED_REASON,
                    reason_id=reason_id,
                    n_calls=len(cc.calls),
                    call_ids=cc.call_ids,
                    started_range=cc.started_range,
                    transcript=cc.transcript,
                )
            )
    logger.info("Closed-lost deals joined to a transcript: {n}", n=len(rows))
    return rows


def _write_audit_csv(rows: list[AuditRow], out_dir: Path) -> Path:
    path = out_dir / "close_reason_audit.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["deal_id", "assigned_by_id", "manager_id", "client_key", "close_reason",
             "reason_id", "n_calls", "call_ids", "started_range", "transcript"]
        )
        for r in rows:
            w.writerow(
                [r.deal_id, r.assigned_by_id, r.manager_id, r.client_key, r.close_reason,
                 r.reason_id, r.n_calls, " ".join(map(str, r.call_ids)), r.started_range, r.transcript]
            )
    return path


# --- Stage 2: LLM judge -----------------------------------------------------
# Prompt/schema and the per-row Claude call live in AtamuraOKK.audit.judge so this
# offline probe and the standing audit pass can never drift apart.


async def judge_rows(rows: list[AuditRow]) -> list[dict[str, Any]]:
    """Stage 2: ask Claude whether each transcript supports the stated reason."""
    client = build_judge_client()
    model = settings.anthropic_scoring_model
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(r: AuditRow) -> dict[str, Any]:
        verdict = await judge_one(
            client,
            transcript=r.transcript,
            close_reason=r.close_reason,
            model=model,
            sem=sem,
        )
        return {"row": r, **verdict}

    logger.info("Judging {n} rows with {m}", n=len(rows), m=model)
    return list(await asyncio.gather(*(one(r) for r in rows)))


def _write_verdicts_csv(judged: list[dict[str, Any]], out_dir: Path) -> Path:
    path = out_dir / "close_reason_verdicts.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["deal_id", "manager_id", "close_reason", "verdict", "confidence",
             "justification", "evidence_quote", "call_ids"]
        )
        for j in judged:
            r: AuditRow = j["row"]
            w.writerow(
                [r.deal_id, r.manager_id, r.close_reason, j["verdict"], j["confidence"],
                 j["justification"], j["evidence_quote"], " ".join(map(str, r.call_ids))]
            )
    return path


def _print_summary(judged: list[dict[str, Any]]) -> None:
    counts: dict[str, int] = defaultdict(int)
    for j in judged:
        counts[j["verdict"]] += 1
    print("\n" + "=" * 60)
    print("CLOSE-REASON AUDIT — verdict counts")
    print("=" * 60)
    for v in (*_VERDICTS, "error"):
        if counts.get(v):
            print(f"  {v:<18}: {counts[v]}")

    # Per-manager contradicted rate — the "labeled honestly?" headline.
    by_mgr: dict[Any, list[str]] = defaultdict(list)
    for j in judged:
        by_mgr[j["row"].manager_id].append(j["verdict"])
    print("\nContradicted (mismatch) rate by manager_id:")
    print(f"  {'manager_id':<12}{'n':>5}{'contradicted':>14}{'rate':>8}")
    for mgr, verdicts in sorted(by_mgr.items(), key=lambda kv: (kv[0] is None, kv[0])):
        n = len(verdicts)
        bad = sum(1 for v in verdicts if v == "contradicted")
        print(f"  {str(mgr):<12}{n:>5}{bad:>14}{(bad / n * 100):>7.1f}%")
    print("=" * 60 + "\n")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50, help="distinct clients to sample")
    parser.add_argument("--judge", action="store_true", help="run the Claude judge (needs credits)")
    parser.add_argument("--out-dir", type=Path, default=Path("exports"))
    args = parser.parse_args()

    if not _REASON_FIELD:
        raise SystemExit("companion_closed_reason_field is empty — set it in .env first.")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = await build_audit_rows(args.limit)
    if not rows:
        print("No closed-lost deals with a transcript in this sample.")
        return

    audit_path = _write_audit_csv(rows, args.out_dir)
    print(f"Review CSV written: {audit_path}  ({len(rows)} rows)")

    if args.judge:
        judged = await judge_rows(rows)
        verdicts_path = _write_verdicts_csv(judged, args.out_dir)
        print(f"Verdicts CSV written: {verdicts_path}")
        _print_summary(judged)


if __name__ == "__main__":
    asyncio.run(main())
