# Phase 1 — Ingestion

Pulls calls from Bitrix into Postgres + object storage, scoped to the calls we
actually analyze (**first call per client AND qualified** ≈ 200/day).

## Flow

```
voximplant.statistic.get  (incremental, cursor = last CALL_START_DATE)
  → keep answered + recorded calls          (CALL_FAILED_CODE=200, dur≥15s, has recording)
  → upsert Call (idempotent on bitrix_call_id)
  → attribute Manager (user.get; degrades without 'user' scope)
  → mark first-call per client_key
  → qualification check (CRM rule; pluggable)
  → analyzable = first_call AND qualified    → status NEW   (else SKIPPED + reason)
  → download analyzable recordings → MinIO   → status DOWNLOADED
```

A **client** is the CRM entity on the call (`CONTACT:123` / `LEAD:45`), falling
back to the normalized phone (`PHONE:7…`). Every answered+recorded call is stored
(slim) so first-call can be computed; only **analyzable** calls get audio
downloaded/transcribed/scored, keeping volume at ~200/day.

### What "first call" means (confirmed)
"First call" = the client's **first *analyzable* call** — earliest call that is
answered (`CALL_FAILED_CODE=200`), ≥ `ingest_min_duration_sec` (15s), and has a
recording. Missed / unanswered / declined attempts (codes 304/480/603/486/404 —
no recording, ~0s) are filtered out *before* ranking, so the first real pickup is
the first call, not the missed attempts. Short (<15s) or unrecorded pickups are
likewise skipped (you can't score them), so a later substantive call may be the
"first" one.

> Note: first-call is computed from calls already in the DB, so it's exact going
> forward from the cursor. For clients whose earlier history predates the initial
> backfill window, a one-time deeper backfill would be needed for full accuracy.

## Components
- `AtamuraOKK/ingestion/service.py` — incremental pull, upsert, first-call,
  qualification, manager attribution, scope/status.
- `AtamuraOKK/ingestion/managers.py` — PORTAL_USER_ID → Manager/Department.
- `AtamuraOKK/ingestion/qualification.py` — `QualificationChecker` (pluggable);
  `CrmStatusQualificationChecker` reads lead STATUS_ID / deal STAGE_ID.
- `AtamuraOKK/ingestion/download.py` — recordings → object storage.
- `AtamuraOKK/storage/` — `ObjectStorage` interface + S3/MinIO impl.

## Run

```bash
make up                 # Postgres + MinIO (db on host :5433 to avoid clashes)
make migrate            # alembic upgrade head
make ingest             # one incremental pull
make ingest-download    # download analyzable recordings
make ingest-run         # ingest + download
make ingest-schedule    # run now, then every 3h
```

## Webhook scopes (IMPORTANT)

The inbound webhook needs **all four** scopes on one token:

| Scope | Used for |
|---|---|
| `telephony` | `voximplant.statistic.get` (pull calls) |
| `disk` | `disk.file.get` — recordings stored as Bitrix Drive files (most calls) |
| `user` | `user.get` — manager name/email/department mapping |
| `crm` | qualification (lead status / deal stage) |

Without `user`, managers are created un-enriched and **backfilled automatically**
on the next run once the scope is added. Without `disk`, only calls with a direct
`CALL_RECORD_URL` download.

## Qualification rule (defined)

A client is **qualified** when one of their deals has **ever entered** the Kanban
column **"Лид квалифицирован"** (faithful to "the manager moved the card there").

- Implemented in `ContactDealStageQualificationChecker`: Contact → `crm.deal.list`
  → `crm.stagehistory.list`, qualified iff any deal has a history entry in a
  qualified stage. This correctly **excludes** deals dropped to *Отказ/LOSE*
  before ever qualifying (verified against live data).
- The qualified stage IDs are **auto-discovered by name** across all deal
  pipelines (currently `PREPARATION` and `C24:PREPAYMENT_INVOIC`); override with
  `qualified_deal_stage_ids` or change `qualified_stage_name`.
- `ingest_require_qualified=True`, so `analyzable = is_first_call AND qualified`.

### Requalification over time
Clients qualify *after* their first call, so `make ingest-requalify` (run as part
of `ingest-run` / `ingest-schedule`) re-checks pending first-calls and promotes
newly-qualified ones from `SKIPPED(not_qualified)` → `NEW`.

Validated live: scanned 200 → 52 answered+recorded → **26 analyzable** (first +
qualified), 23 not_qualified, 17 not_first_call.
