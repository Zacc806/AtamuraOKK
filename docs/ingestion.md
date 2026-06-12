# Phase 1 — Ingestion

Pulls calls from Bitrix into Postgres + object storage, scoped to the calls we
actually analyze: **every answered, recorded call ≥90s until the client enters
«Лид квалифицирован»** (≈110–120/day).

## Flow

```
voximplant.statistic.get  (incremental, cursor = last CALL_START_DATE)
  → keep answered + recorded calls          (CALL_FAILED_CODE=200, dur≥90s, has recording)
  → upsert Call (idempotent on bitrix_call_id)
  → attribute Manager (user.get; degrades without 'user' scope)
  → resolve qualification moment (earliest qualified-stage entry; pluggable)
  → analyzable = started_at ≤ qualified_at (or never/unknown qualified)
                                              → status NEW   (else SKIPPED + reason)
  → download analyzable recordings → MinIO    → status DOWNLOADED
```

A **client** is the CRM entity on the call (`CONTACT:123` / `LEAD:45`), falling
back to the normalized phone (`PHONE:7…`). Every answered+recorded call is stored
(slim); only **analyzable** calls get audio downloaded/transcribed/scored.

### The scope rule (changed 2026-06-12, applied forward-only)
A call is analyzable until its client qualifies: calls after the qualification
moment (`client_qualified_at`, the earliest `crm.stagehistory.list` entry into a
qualified stage) are visit logistics, not sales conversations — skipped as
`after_qualification`. Clients who **never** qualify (or can't be resolved —
phone-only) stay fully in scope: those failed conversations are exactly what QA
needs to see. The previous rule (*first call per client AND qualified*) was
retired **forward-only** by operator decision: rows it skipped
(`not_first_call` / `not_qualified` / `qualification_unknown`) are frozen and
never reopened (`_LEGACY_SKIP_REASONS` in `service.py`).

`is_first_call` is still computed and stored as a data point, but no longer
gates scope. The periodic `refresh_qualification` pass inverted accordingly: it
no longer promotes skipped calls — it re-checks recent unclaimed NEW calls so a
late-arriving qualification skips their post-qualification calls before they
are claimed and scored.

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
  → `crm.stagehistory.list`, returning the **earliest** qualified-stage entry
  time (`Qualification.at`) — the scope boundary. Deals dropped to *Отказ/LOSE*
  before ever qualifying correctly never produce a moment (verified live).
- The qualified stage IDs are **auto-discovered by name** across all deal
  pipelines (currently `PREPARATION` and `C24:PREPAYMENT_INVOIC`); override with
  `qualified_deal_stage_ids` or change `qualified_stage_name`.
- `ingest_until_qualified=True` gates the `after_qualification` skip; set False
  to score everything regardless of qualification.

### Requalification over time
A client can qualify *after* some of their calls were already ingested as
analyzable — that needs no action (those calls were before the moment). The
periodic `make ingest-requalify` (run as part of `ingest-run` /
`ingest-schedule`) handles the inverse race: it re-checks recent unclaimed NEW
calls whose client has no known qualification yet, so a late-arriving
qualification skips their post-qualification calls before they are scored.
