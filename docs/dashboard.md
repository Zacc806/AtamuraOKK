# Phase 4 — Dashboard (Metabase)

The core deliverable: each department head sees QA analysis per manager in their
department. Metabase runs in Docker over Postgres and reads the two reporting
views (`call_scores_latest`, `call_criteria_latest`).

## Stand it up
```bash
make metabase-up                                   # start Metabase (http://localhost:3000)
METABASE_ADMIN_EMAIL=you@atamura.kz \
METABASE_ADMIN_PASSWORD='Str0ng-Passw0rd!' \
  make metabase-bootstrap                          # create admin + connect Postgres
```
`bootstrap.py` is idempotent (login-first) and connects the **Atamura QA** data
source. Metabase reaches Postgres on the internal hostname **`AtamuraOKK-db:5432`**
(not `localhost:5433`).

> Dev uses Metabase's H2 app-db (volume `AtamuraOKK-metabase-data`). For
> production, switch the app-db to Postgres (`MB_DB_TYPE=postgres` + `MB_DB_*` to a
> dedicated database) in `docker-compose.yml`.

## Build the dashboards
Each file in `metabase/queries/` is a ready native (SQL) question. Create them as
questions, then arrange into dashboards:

| Dashboard | Cards (queries) |
|---|---|
| **Department roll-up** | `02_department_rollup`, `04_zone_distribution`, `05_score_histogram`, `07_block_distribution`, `06_team_weakest_criteria` |
| **Per-manager scorecard** | `01_per_manager_scorecard`, `03_manager_trend_weekly` |
| **Call drill-down** | `09_call_drilldown`, `10_call_criteria_drilldown` (both filtered by `{{call_id}}`) |
| **Flagged-calls queue** | `08_flagged_calls_queue` (+ click-through to drill-down) |
| **Pipeline coverage** | `11_pipeline_funnel` |

Queries with `[[AND department_name = {{department_name}}]]` expose an optional
Metabase **filter**; wire a dashboard filter to it. The drill-down click-through:
on the flagged queue / scorecard, set the `call_id` column to link to the
drill-down dashboard passing `call_id`.

Audio playback: the drill-down exposes `audio_object_key` (object-storage key) and
a `bitrix_crm_url` to open the contact/recording in Bitrix. For in-dashboard
playback, generate a MinIO presigned URL (see `storage.presigned_url`) via a small
field-formatting or a companion endpoint.

## Department row-level access (the original requirement)

**Goal:** a department head sees only rows where `department_name` matches theirs.

### Option A — Metabase Pro/Enterprise (true row-level security)
Use **data sandboxing**:
1. Admin → People → add a **user attribute** `department_name` to each head
   (e.g. `Department 250`).
2. Admin → Permissions → Atamura QA → for the head's **group**, sandbox both views
   on `department_name = {{department_name}}` (column ↔ attribute).
3. Every question built on the views is then automatically filtered per user — no
   per-department duplication.

### Option B — Open-source Metabase (no sandboxing)
OSS has no row-level sandboxing, so isolate by collection:
1. One **group** + one **collection** per department.
2. Duplicate the dashboards per department with the `department_name` filter
   **locked** to that department (the queries already support the filter).
3. Grant each group view access only to its own collection, and restrict raw SQL
   ("native query") access so heads can't bypass the filter.

This is isolation-by-collection, not hard row-level security; if heads must never
see other departments' data and can't be trusted with SQL, use Option A.

> Note: department **names** currently show as `Department <id>` until the Bitrix
> webhook gets the `department` scope (then `user.get`/`department.get` backfills
> real names). Grouping/filtering works regardless.

## Verified
Metabase is up (`/api/health` → ok), the **Atamura QA** Postgres source is
connected, and both reporting views are visible to Metabase. All 11 queries run
against the live data.
