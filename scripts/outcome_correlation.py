"""One-off money-anchor probe: does call QA quality predict a real CRM visit?

Read-only, no schema change. For every SCORED *qualification* call we resolve the
client's deals and ask Bitrix whether they ever reached the conducted-visit stage
«Фактический визит» (``C24:WON``) — the telemarketer's КЭВ (key target action).
Then we report:

  1. Coverage — how many scored calls resolve to a deal / an outcome.
  2. Base visit rate, and visit rate by overall-score quartile.
  3. Visit rate by the «Закрытие на КЭВ» (closing) block quartile — the LLM's
     implicit "did the manager book the meeting" signal.
  4. Validation of that LLM signal against the CRM fact (confusion + lift).

Resolution reuses the Contact→deals→stage-history pattern from
``ingestion/qualification.py`` and ``web/api/v1/day.py``. Run on the host:

    uv run python scripts/outcome_correlation.py

Caveats (printed in the footer): the SCORED set is a subset (May–mid-Jun), and
pre-June calls carry the mono-diarization caveat — whole-call signals like the
closing block are less affected than role/channel slices, but note it.
"""

from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import text

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.settings import settings

# Zvandau (cat 24) outcome stages — STATUS_IDs are portal-stable (from day.py).
VISIT_STAGE = settings.companion_meeting_stage_id  # "C24:WON" — Фактический визит
SCHEDULED_STAGES = {
    "C24:EXECUTING",  # Записан на встречу
    "C24:FINAL_INVOICE",  # Визит подтверждён
}
NO_SHOW_STAGE = "C24:UC_9OBT14"  # Не дошёл до встречи
OUTCOME_STAGES = sorted({VISIT_STAGE, *SCHEDULED_STAGES, NO_SHOW_STAGE})

# Lead category «Квалификация клиента» — read LIVE off the deal (the stored
# calls.client_category is stale for the May–Jun scored set). Same field/map as
# ingestion/category.py, so letters match the rest of the pipeline.
_CAT_FIELD = settings.client_category_field
_CAT_MAP = settings.client_category_value_map

_CONCURRENCY = 6
_OUT_CSV = Path("exports/outcome_correlation.csv")

ROWS_SQL = text(
    """
    SELECT c.id                                              AS call_id,
           c.client_key                                      AS client_key,
           c.crm_entity_type                                 AS crm_entity_type,
           c.crm_entity_id                                   AS crm_entity_id,
           c.manager_id                                      AS manager_id,
           c.started_at                                      AS started_at,
           (s.criteria->>'percent')::float                   AS percent,
           (s.criteria->'blocks'->'closing'->>'score')::float AS closing_score,
           (s.criteria->'blocks'->'closing'->>'max')::float   AS closing_max
    FROM scores s
    JOIN calls c ON c.id = s.call_id
    WHERE s.criteria->>'call_type' = 'квалификация'
    """
)


@dataclass
class Outcome:
    resolved: bool  # client mapped to >=1 deal
    visited: bool = False  # ever reached C24:WON
    scheduled: bool = False  # reached any appointment stage (set/confirmed/no-show/visit)
    no_show: bool = False  # reached no-show and never visited
    visit_at: datetime | None = None
    category: str | None = None  # A/B/C/X lead category off the latest tagged deal


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


async def _resolve_deals(
    bx: BitrixClient, entity_type: str, entity_id: str
) -> tuple[list[str], str | None]:
    """Client's deal ids + lead category, in one ``crm.deal.list`` call.

    Category = the letter off the most recent deal that carries one (newest-first,
    mirroring ``ingestion/category.py``). Returns ([], None) for LEAD/PHONE-only.
    """
    if entity_type == "CONTACT":
        filter_: dict[str, Any] = {"CONTACT_ID": entity_id}
    elif entity_type == "COMPANY":
        filter_ = {"COMPANY_ID": entity_id}
    elif entity_type == "DEAL":
        filter_ = {"ID": entity_id}
    else:  # LEAD / PHONE-only — not resolvable to deal stages
        return [], None

    select = ["ID", _CAT_FIELD] if _CAT_FIELD else ["ID"]
    ids: list[str] = []
    category: str | None = None
    async for d in bx.list(
        "crm.deal.list", {"filter": filter_, "select": select, "order": {"ID": "DESC"}}
    ):
        ids.append(str(d["ID"]))
        if category is None and _CAT_FIELD:
            category = _CAT_MAP.get(str(d.get(_CAT_FIELD) or "").strip())
    return ids, category


async def _outcome_for_deals(bx: BitrixClient, deal_ids: list[str]) -> Outcome:
    """Scan stage history of the client's deals for the outcome stages."""
    reached: set[str] = set()
    visit_at: datetime | None = None
    cursor: int | None = 0
    while cursor is not None:
        env = await bx.call_raw(
            "crm.stagehistory.list",
            {
                "entityTypeId": 2,  # deals
                "filter": {"OWNER_ID": deal_ids, "STAGE_ID": OUTCOME_STAGES},
                "select": ["STAGE_ID", "CREATED_TIME"],
                "start": cursor,
            },
        )
        result = env.get("result") or {}
        items = result.get("items") if isinstance(result, dict) else result
        for it in items or []:
            sid = str(it.get("STAGE_ID") or "")
            reached.add(sid)
            if sid == VISIT_STAGE:
                t = _parse_dt(it.get("CREATED_TIME"))
                if t and (visit_at is None or t < visit_at):
                    visit_at = t
        nxt = env.get("next")
        cursor = int(nxt) if nxt is not None else None

    visited = VISIT_STAGE in reached
    scheduled = visited or bool(reached & SCHEDULED_STAGES) or NO_SHOW_STAGE in reached
    no_show = (NO_SHOW_STAGE in reached) and not visited
    return Outcome(
        resolved=True,
        visited=visited,
        scheduled=scheduled,
        no_show=no_show,
        visit_at=visit_at,
    )


async def _resolve_client(
    bx: BitrixClient, client_key: str, sem: asyncio.Semaphore
) -> Outcome:
    entity_type, _, entity_id = client_key.partition(":")
    async with sem:
        try:
            deal_ids, category = await _resolve_deals(bx, entity_type, entity_id)
            if not deal_ids:
                return Outcome(resolved=False, category=category)
            outcome = await _outcome_for_deals(bx, deal_ids)
            outcome.category = category
            return outcome
        except BitrixError as exc:
            logger.warning("Outcome lookup failed for {k}: {e}", k=client_key, e=exc)
            return Outcome(resolved=False)


# --- tiny stats helpers (no numpy/pandas dependency) ------------------------


def _quartile_buckets(values: list[float]) -> list[float]:
    """Three cut points (q25, q50, q75) over a sorted copy of values."""
    if not values:
        return [0.0, 0.0, 0.0]
    s = sorted(values)
    return [s[int(len(s) * q)] for q in (0.25, 0.50, 0.75)]


def _bucket_of(value: float, cuts: list[float]) -> int:
    return sum(1 for c in cuts if value >= c)  # 0..3


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    return num / (dx * dy) if dx and dy else None


def _rate(num: int, den: int) -> str:
    return f"{num/den*100:5.1f}%" if den else "   n/a"


def _pct(x: float | None) -> str:
    return f"{x*100:5.1f}%" if x is not None else "   n/a"


def _category_report(resolved: list[dict[str, Any]]) -> None:
    """Visit rate per lead category, and closing-% split WITHIN each category.

    The lead-quality control: if the high-closing half still beats the low-closing
    half *within* a category (positive gap), the QA score adds signal beyond who
    the lead was — i.e. it's measuring the manager, not just the lead.
    """
    print("\nLead-quality control — visit rate by category, split at the")
    print("category's own closing-% median (gap = skill signal beyond lead):")
    header = f"  {'cat':<7}{'n':>5}{'base':>8}{'close<med':>11}{'close>=med':>12}{'gap':>9}"
    print(header)
    for cat in ("A", "B", "C", "X", None):
        items = [r for r in resolved if r["category"] == cat]
        n = len(items)
        if n == 0:
            continue
        base = sum(1 for r in items if r["visited"]) / n
        cl = [r for r in items if r["closing_pct"] is not None]
        low_r = high_r = gap = None
        if len(cl) >= 4:
            med = sorted(r["closing_pct"] for r in cl)[len(cl) // 2]
            low = [r for r in cl if r["closing_pct"] < med]
            high = [r for r in cl if r["closing_pct"] >= med]
            if low and high:
                low_r = sum(1 for r in low if r["visited"]) / len(low)
                high_r = sum(1 for r in high if r["visited"]) / len(high)
                gap = high_r - low_r
        label = cat or "(none)"
        gap_s = f"{gap*100:+5.1f}pp" if gap is not None else "   n/a"
        print(
            f"  {label:<7}{n:>5}{_pct(base):>8}{_pct(low_r):>11}{_pct(high_r):>12}{gap_s:>9}"
        )


async def main() -> None:
    async with session_scope() as session:
        rows = (await session.execute(ROWS_SQL)).mappings().all()
    logger.info("Scored qualification calls: {n}", n=len(rows))

    # Resolve each distinct client once (calls are ~1:1 with clients here).
    client_keys = sorted({r["client_key"] for r in rows if r["client_key"]})
    sem = asyncio.Semaphore(_CONCURRENCY)
    async with BitrixClient() as bx:
        results = await asyncio.gather(
            *(_resolve_client(bx, k, sem) for k in client_keys)
        )
    outcomes: dict[str, Outcome] = dict(zip(client_keys, results, strict=True))
    logger.info("Resolved {n} distinct clients against Bitrix", n=len(outcomes))

    # --- assemble per-call records ---
    records: list[dict[str, Any]] = []
    for r in rows:
        oc = outcomes.get(r["client_key"] or "", Outcome(resolved=False))
        closing_pct = (
            r["closing_score"] / r["closing_max"]
            if r["closing_score"] is not None and r["closing_max"]
            else None
        )
        records.append(
            {
                "call_id": r["call_id"],
                "manager_id": r["manager_id"],
                "started_at": r["started_at"],
                "client_key": r["client_key"],
                "category": oc.category,
                "percent": r["percent"],
                "closing_pct": closing_pct,
                "resolved": oc.resolved,
                "scheduled": oc.scheduled,
                "no_show": oc.no_show,
                "visited": oc.visited,
                "visit_at": oc.visit_at,
            }
        )

    resolved = [r for r in records if r["resolved"]]
    visited_after = sum(
        1
        for r in resolved
        if r["visited"]
        and r["visit_at"]
        and r["started_at"]
        and r["visit_at"] >= r["started_at"]
    )

    # --- report ---
    n_all = len(records)
    n_res = len(resolved)
    n_visit = sum(1 for r in resolved if r["visited"])
    n_sched = sum(1 for r in resolved if r["scheduled"])
    n_noshow = sum(1 for r in resolved if r["no_show"])

    print("\n" + "=" * 72)
    print("MONEY ANCHOR — call QA quality vs real CRM visit «Фактический визит»")
    print("=" * 72)
    print(f"Scored qualification calls       : {n_all}")
    print(f"  resolved to >=1 deal           : {n_res}  ({_rate(n_res, n_all)})")
    print(f"  unresolved (phone/lead only)   : {n_all - n_res}")
    print("-" * 72)
    print(f"Reached an appointment stage     : {n_sched}  ({_rate(n_sched, n_res)})")
    print(f"  of those, no-show (не дошёл)   : {n_noshow}  ({_rate(n_noshow, n_res)})")
    print(f"CONDUCTED VISIT (C24:WON)         : {n_visit}  ({_rate(n_visit, n_res)})  <- base rate")
    print(f"  visit dated after the call     : {visited_after}/{n_visit}")

    # By overall-score quartile.
    pct_vals = [r["percent"] for r in resolved if r["percent"] is not None]
    pct_cuts = _quartile_buckets(pct_vals)
    print("\nVisit rate by OVERALL score quartile (resolved calls):")
    print(f"  quartile cuts (percent): {[round(c,1) for c in pct_cuts]}")
    _print_buckets(resolved, "percent", pct_cuts, n_visit, n_res)

    # By closing-block («Закрытие на КЭВ») quartile — the LLM "booked" proxy.
    cl_vals = [r["closing_pct"] for r in resolved if r["closing_pct"] is not None]
    cl_cuts = _quartile_buckets(cl_vals)
    print("\nVisit rate by «Закрытие на КЭВ» (closing-block) quartile:")
    print(f"  quartile cuts (closing %): {[round(c,2) for c in cl_cuts]}")
    _print_buckets(resolved, "closing_pct", cl_cuts, n_visit, n_res)

    # Correlations.
    cl_pairs = [
        (r["closing_pct"], 1.0 if r["visited"] else 0.0)
        for r in resolved
        if r["closing_pct"] is not None
    ]
    pct_pairs = [
        (r["percent"], 1.0 if r["visited"] else 0.0)
        for r in resolved
        if r["percent"] is not None
    ]
    r_cl = _pearson([a for a, _ in cl_pairs], [b for _, b in cl_pairs])
    r_pct = _pearson([a for a, _ in pct_pairs], [b for _, b in pct_pairs])
    print("\nPoint-biserial correlation with conducted visit:")
    print(f"  closing-block %  vs visit : r = {r_cl:+.3f}" if r_cl is not None else "  closing: n/a")
    print(f"  overall score %  vs visit : r = {r_pct:+.3f}" if r_pct is not None else "  overall: n/a")

    # LLM-"booked" validation: predict booked = closing in top quartile.
    if cl_vals:
        thr = cl_cuts[2]  # top-quartile cutoff
        pred = [r for r in resolved if (r["closing_pct"] or 0) >= thr]
        tp = sum(1 for r in pred if r["visited"])
        fp = len(pred) - tp
        fn = sum(1 for r in resolved if r["visited"] and (r["closing_pct"] or 0) < thr)
        base = n_visit / n_res if n_res else 0
        prec = tp / len(pred) if pred else 0
        lift = (prec / base) if base else 0
        print(
            f"\nLLM-«booked» proxy = closing% >= top quartile ({thr:.2f}):"
        )
        print(f"  predicted booked (n)           : {len(pred)}")
        print(f"  precision (visited | booked)   : {_rate(tp, len(pred))}")
        print(f"  recall    (booked | visited)   : {_rate(tp, tp + fn)}")
        print(f"  lift over base visit rate      : {lift:.2f}x")

    # Lead-quality control: does closing still predict visits WITHIN a category?
    _category_report(resolved)

    # CSV dump for ad-hoc slicing.
    _OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with _OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "call_id", "manager_id", "started_at", "client_key", "category",
                "percent", "closing_pct", "resolved", "scheduled",
                "no_show", "visited", "visit_at",
            ]
        )
        for r in records:
            w.writerow(
                [
                    r["call_id"], r["manager_id"], r["started_at"], r["client_key"],
                    r["category"], r["percent"], r["closing_pct"], r["resolved"],
                    r["scheduled"], r["no_show"], r["visited"], r["visit_at"],
                ]
            )
    print(f"\nPer-call CSV written: {_OUT_CSV}")
    print("-" * 72)
    print("CAVEATS: SCORED set is a subset (May–mid-Jun, all zone='risk'); pre-June")
    print("calls carry the mono-diarization caveat; visit attribution = any C24:WON")
    print("transition on the client's deals (survives the cat-2 move on visit).")
    print("=" * 72 + "\n")


def _print_buckets(
    resolved: list[dict[str, Any]],
    key: str,
    cuts: list[float],
    n_visit: int,
    n_res: int,
) -> None:
    base = n_visit / n_res if n_res else 0
    labels = ["Q1 (low) ", "Q2       ", "Q3       ", "Q4 (high)"]
    buckets: dict[int, list[dict[str, Any]]] = {0: [], 1: [], 2: [], 3: []}
    for r in resolved:
        v = r[key]
        if v is None:
            continue
        buckets[_bucket_of(v, cuts)].append(r)
    print(f"  {'bucket':<10} {'n':>5} {'visit':>7} {'sched':>7} {'lift':>6}")
    for b in range(4):
        items = buckets[b]
        n = len(items)
        nv = sum(1 for r in items if r["visited"])
        ns = sum(1 for r in items if r["scheduled"])
        lift = (nv / n / base) if (n and base) else 0
        print(
            f"  {labels[b]:<10} {n:>5} {_rate(nv, n):>7} {_rate(ns, n):>7} {lift:>5.2f}x"
        )


if __name__ == "__main__":
    asyncio.run(main())
