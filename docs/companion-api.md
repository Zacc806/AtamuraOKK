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

## Auth — two layers

1. **Service bearer.** Every `/api/v1/*` request must carry
   `Authorization: Bearer <token>` matching `ATAMURAOKK_COMPANION_API_TOKEN`.
   The companion's nginx injects it server-side, so the browser never holds it.
   **Fail-closed:** if the token is unset the API returns `503` rather than
   serving data unauthenticated.
2. **Personal user key** (`X-Companion-User-Key`, sent by the browser). Two
   sources, checked in order:
   - the **static РОП key** — `ATAMURAOKK_COMPANION_HEAD_KEY`. A fixed code set
     once in the environment; logging in with it grants `head` with no
     `companion_users` row. Compared in constant time; empty = disabled.
   - a `companion_users` row (only the SHA-256 of the key is stored) carrying
     the **role**:
     - `manager` — scoped to their own `bitrix_user_id`: own
       scorecard/calls/day and only their own calls' feedback; anything else is
       `403`. The team rollup is `403`.
     - `head` — руководитель отдела продаж: every manager + the team rollup.
       A head row may additionally carry a **`department_id`** (Bitrix
       department id) — an **office РОП** scoped to that one department: only
       managers whose `managers` row maps to it (unknown/unenriched managers
       and unattributed meetings stay global-head-only), only their own
       `/teams/{id}/summary`, and access management limited to their own
       department's **manager** keys. `department_id = NULL` keeps the head
       global.

   Missing/invalid/revoked key → `401`. Access management is head-tiered:
   any head issues and revokes **manager** keys from the cabinet (`/users`
   endpoints below; a scoped head only within their department — and issuing
   one **ties the manager to that department**: the `managers` row is
   get-or-created, pointed at the head's department and marked `enriched`,
   so ingestion never re-derives it — the cabinet's word beats Bitrix's).
   The **global** head additionally mints and revokes department-scoped head
   keys (office РОПы) from the cabinet (`{role: "head", department_id, name?
   | bitrix_user_id?}`). The CLI `python -m AtamuraOKK.companion_users
   create|list|revoke` remains the fallback for everything and the only way
   to create a *global* (dept-less) head row or reactivate a key; the raw key
   is shown once at creation in every flow. The cabinet can never mint or
   revoke a global head — the static key lives only in the environment.

## Identifiers

Path params are **Bitrix** ids, since the companion is a Bitrix24 app and holds
those, not AtamuraOKK's internal row ids:

- `manager_id` → Bitrix user id (`managers.bitrix_user_id`)
- `department_id` → Bitrix department id (`departments.bitrix_id`)
- `call_id` (feedback only) → AtamuraOKK internal call id (from the call feed)

### CRM card deep links (`bitrix_url`)

The call feed, call feedback, and the Мой день action items each carry a
`bitrix_url` — a deep link to the entity's Bitrix24 CRM card, so the cabinet can
jump straight from a scored call (or a "кому звонить" deal) into Bitrix. OKK is
the single Bitrix gateway, so it builds the URL; the companion only renders it.
It is **read-only navigation** (no Bitrix write), shaped as
`{portal_origin}/crm/{lead|deal|contact|company}/details/{id}/`. The portal
origin is derived from OKK's `BITRIX_WEBHOOK` (scheme+host); calls are linked via
their `CRM_ENTITY_TYPE`/`CRM_ENTITY_ID`, Мой день actions via the deal id.
`bitrix_url` is `null` when the webhook is unset or the call has no CRM entity —
the cabinet then simply hides the link.

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
| GET | `/api/v1/me` | who am I — role + linked manager profile + `department` scope (manager's own dept, or the dept a scoped head is limited to; null for the global head). The cabinet boots from this |
| GET | `/api/v1/managers/{manager_id}/scorecard?period=YYYY-MM` | ОКК scorecard (`okk` + `zone_distribution` + `meetings` + null `money`) |
| GET | `/api/v1/managers/{manager_id}/calls?since=&limit=` | Звонки feed — scored calls, newest first; each carries `bitrix_url` (deep link to the call's CRM card, null when not derivable) |
| GET | `/api/v1/calls/{call_id}/feedback` | авто-разбор за 90 сек — summary/strengths/growth/criteria + `bitrix_url` (CRM-card deep link) + `transcript` (speaker-labeled blocks: `agent`/`customer`, coalesced; falls back to one `unknown` block from `full_text`) |
| GET | `/api/v1/managers/{manager_id}/meetings?since=&limit=` | Встречи feed — scored ОП meetings, newest first |
| GET | `/api/v1/meetings/{meeting_id}/feedback` | авто-разбор for one meeting — score/tone/red flags/criteria |
| GET | `/api/v1/managers/{manager_id}/feed?since=&limit=` | unified Звонки+Встречи feed — kind-tagged items, newest first |
| GET | `/api/v1/rubrics` | active criteria set per `source` (`"tm"` calls / `"op"` meetings) |
| GET | `/api/v1/teams/{department_id}/summary?period=YYYY-MM` | РОП-вид — per-manager roster + group rollup, calls **and** meetings (**head only**; a scoped head only their own department) |
| GET | `/api/v1/departments` | departments (`{bitrix_id, name}`, name-sorted) for the office-РОП assignment dropdown; names lazily backfilled from Bitrix `department.get` (**global head only**) |
| GET | `/api/v1/users` | cabinet users — all for the global head; a scoped head sees only their own department's manager keys (**head only**) |
| POST | `/api/v1/users` | issue a key; raw key returned once. Manager (`{bitrix_user_id, name?}`): any head — a scoped head's manager is tied to their department. Head (`{role: "head", department_id, name? \| bitrix_user_id?}`): **global head only** |
| POST | `/api/v1/users/{id}/revoke` | deactivate a key. Global head: manager + scoped-head keys (dept-NULL head rows are `403`, env/CLI-only); scoped head: own department's manager keys only |

`period` defaults to the current month in `report_timezone`; a malformed value
returns `422`. Unknown manager/department/call returns `404`; a manager asking
for anyone but themselves gets `403`.

Meetings come from the ОП meeting pipeline (`AtamuraOKK/scoring/meetings/`):
scored recordings are mirrored into the Postgres `meetings` table by its
`push` stage and attributed to **whoever uploaded the recording** to the
"Встречи ОП" Disk folder (`uploaded_by_bitrix_id`), so `manager_id` in the
meetings paths is that uploader's Bitrix user id. A meeting with no usable
uploader is visible to the head only. Each row carries a `source` tag
(`"op"` today) so other departments' recordings can be distinguished later.

### Per-department items and criteria

Meetings live **in the same place as calls** — a department's scored items
are whatever it produces (ТМ → calls, ОП → meetings), and each department
scores against **its own criteria**. The two scoring semantics are
deliberately distinct and never forced into one scale: calls carry
percent/zone/`okk_5`, meetings carry `score_pct`/`passed`.

- `ManagerScorecard` and the team summary's `group` each carry both blocks:
  the existing `okk`/`zone_distribution` (calls) plus a `meetings` aggregate
  (`meetings_scored`, `avg_score_pct`, `passed`, `failed`,
  `needs_human_review`). Whichever a manager/department doesn't produce is
  simply zero/null — no department→type mapping needs maintaining. Note:
  `meetings` on the scorecard is the scored-recordings aggregate, distinct
  from the planned `MoneyAxis.meetings` deal counter.
- The team summary's roster is the **union** of call-managers and
  meeting-managers in the department. Meetings tie into the department via
  the uploader's `managers` row (`manager_id → department_id`), so a meeting
  whose uploader is still an unenriched placeholder (no department yet) is
  invisible in the rollup until ingestion's `ensure_managers` backfills the
  profile — permanent only if the webhook lacks the `user` scope.
- `/managers/{id}/feed` merges both feeds into one kind-tagged list
  (`{kind: "call"|"meeting", at, call?, meeting?}`), newest first, so the
  cabinet renders one screen regardless of department.
- `/rubrics` returns the active criteria set per `source` (one active rubric
  per source, seeded by `make seed-rubric` into `rubric_versions`), letting
  the cabinet show each department's own checklist behind the numbers.

When issuing a key, `name` is optional: omitted, OKK resolves the display
name from the Bitrix user id — first from its own `managers` table (already
enriched from `user.get` by ingestion), else via a live read-only `user.get`.
If neither resolves (bad id, or the webhook lacks the `user` scope) the
request is `422` with a hint to pass `name` explicitly.

The `/users` endpoints are the one writable surface — they write only
AtamuraOKK's own `companion_users` / `managers` / `departments` tables
(never the pipeline state or Bitrix; name resolution only *reads* Bitrix).
Minting a head requires a `department_id`, so a compromised cabinet session
can never create another **global** head.
Schemas live in `web/api/v1/schemas.py`; the live OpenAPI spec is at
`/api/docs`.

## Layout

```
web/api/v1/
  views.py     # endpoints (router, auth dependencies, role checks, status mapping)
  service.py   # read queries over call_scores_latest / call_criteria_latest
  schemas.py   # response DTOs (the contract)
  okk.py       # ОКК 1–5 mapping + YYYY-MM period windows
  auth.py      # service bearer (fail-closed) + personal-key identity/role
companion_users.py        # CLI: issue/list/revoke personal keys
db/models/companion_user.py  # cabinet users (hashed key + role)
db/views.py    # reporting-view DDL shared by Alembic + the test harness
```

## Phase 2 sketch (when the money axis is built)

1. New ingestion concern: read-only `crm.deal.list` (cat 2) + `crm.lead.list`,
   reusing the existing cursor + manager-attribution machinery.
2. New `manager_metrics` table (or materialized view) keyed by manager+period.
3. Fill `MoneyAxis` in `service.get_scorecard` / `get_team_summary` and flip its
   `status`.
