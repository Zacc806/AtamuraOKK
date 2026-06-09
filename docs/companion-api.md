# Companion read API (`/api/v1`)

The read contract the **sales-companion** UX (`/Users/mac01/Documents/sales-companion`)
consumes. AtamuraOKK is the backend of record for everything call-quality; the
companion is a pure consumer — it pulls scorecards/feedback over HTTP and writes
nothing. This is an **anti-corruption layer**: the pipeline's internal `status`
enum and raw table shape never appear in a response, so the pipeline can evolve
behind it.

## Status

**Phase 1 (live now) — call-quality axis.** Backed entirely by data the pipeline
already produces (`scores` → the `call_scores_latest` view). Ships today.

**Phase 2 (deferred) — money axis.** Conversion (meetings ÷ leads), план %,
CRM-дисциплина. Not wired: requires a new `crm.deal.list`/`crm.lead.list`
ingestion concern + a per-manager metrics table, and the numbers are only
trustworthy after the Bitrix data-cleanup gate (see sales-companion
`docs/handoff.md`). The `money` object is **published now with null values** and
`status: "not_available"` so the companion can code against the final shape.

## Auth

Every `/api/v1/*` request must carry `Authorization: Bearer <token>` matching
`ATAMURAOKK_COMPANION_API_TOKEN`. **Fail-closed:** if the token is unset the API
returns `503` rather than serving data unauthenticated.

## Identifiers

Path params are **Bitrix** ids, since the companion is a Bitrix24 app and holds
those, not AtamuraOKK's internal row ids:

- `manager_id` → Bitrix user id (`managers.bitrix_user_id`)
- `department_id` → Bitrix department id (`departments.bitrix_id`)
- `call_id` (feedback only) → AtamuraOKK internal call id (from the call feed)

## ОКК 0–100 → 1–5

The companion needs ОКК as a 1–5 bonus modifier; the pipeline stores a 0–100
percent. The single mapping (`web/api/v1/okk.py`), aligned to the rubric zones:

| percent | ОКК | zone |
|---|---|---|
| ≥ 90 | 5 | strong |
| 85–89 | 4 | strong |
| 80–84 | 3 | normal |
| 75–79 | 2 | borderline |
| < 75 | 1 | risk |

Only genuine qualification calls (`is_qualification_call`) count toward the
score; reminders/vendor/internal/wrong-number calls are excluded.

## Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/api/v1/managers/{manager_id}/scorecard?period=YYYY-MM` | ОКК scorecard (`okk` + `zone_distribution` + null `money`) |
| GET | `/api/v1/managers/{manager_id}/calls?since=&limit=` | Звонки feed — scored calls, newest first |
| GET | `/api/v1/calls/{call_id}/feedback` | авто-разбор за 90 сек — summary/strengths/growth/criteria |
| GET | `/api/v1/teams/{department_id}/summary?period=YYYY-MM` | РОП-вид — per-manager roster + group rollup |

`period` defaults to the current month in `report_timezone`; a malformed value
returns `422`. Unknown manager/department/call returns `404`. Schemas live in
`web/api/v1/schemas.py`; the live OpenAPI spec is at `/api/docs`.

## Layout

```
web/api/v1/
  views.py     # endpoints (router, bearer-token dependency, status mapping)
  service.py   # read queries over call_scores_latest / call_criteria_latest
  schemas.py   # response DTOs (the contract)
  okk.py       # ОКК 1–5 mapping + YYYY-MM period windows
  auth.py      # shared-bearer-token dependency (fail-closed)
db/views.py    # reporting-view DDL shared by Alembic + the test harness
```

## Phase 2 sketch (when the money axis is built)

1. New ingestion concern: read-only `crm.deal.list` (cat 2) + `crm.lead.list`,
   reusing the existing cursor + manager-attribution machinery.
2. New `manager_metrics` table (or materialized view) keyed by manager+period.
3. Fill `MoneyAxis` in `service.get_scorecard` / `get_team_summary` and flip its
   `status`.
