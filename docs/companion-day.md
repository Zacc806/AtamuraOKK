# Companion "Мой день" — live TM-funnel read-through

The sales-companion's **Мой день** screen (кому звонить / встречи / деньги) is
served by `GET /api/v1/managers/{bitrix_user_id}/day`. Unlike the rest of
`/api/v1` (which reads OKK's Postgres call-QA data), this endpoint reads **straight
through to Bitrix** per request, short-TTL cached (`web/api/v1/day.py`).

Why read-through, not a stored ingestion stage: the screen is inherently
real-time ("кому звонить сейчас", "встречи сегодня") and the data is the TM's own
live deal pipeline. OKK still owns the single Bitrix gateway, so the companion
stays a thin consumer — it never calls Bitrix itself.

## Where the data lives (reverse-engineered + operator-confirmed, 2026-06-09)

The telemarketing funnel is Bitrix **deal category 24 ("Zvandau")**, with each deal
owned by the telemarketer via `ASSIGNED_BY_ID`. Its stage names ARE the day
signals (`STATUS_ID` → meaning):

| STATUS_ID | Stage name | Day signal (bucket) |
|---|---|---|
| `C24:NEW` | Новая заявка | обработать |
| `C24:PREPARATION` | Взято в работу | двигать к встрече |
| `C24:UC_OPEENZ` | Попросил перезвонить | hot callback |
| `C24:UC_VL3EHH` | Недозвон 1 | no_answer |
| `C24:UC_LS7DKY` | Недозвон 2 | no_answer |
| `C24:PREPAYMENT_INVOIC` | Лид квалифицирован | записать на встречу |
| `C24:EXECUTING` | Записан на встречу в ОП | meetings |
| `C24:FINAL_INVOICE` | Подтвержден визит | meetings |
| `C24:UC_9OBT14` | Не дошёл до встречи | cooling |
| `C24:UC_8PKXOA` | Дубль | проверить |
| `C24:UC_5UCLAR` | Встреча без ТМ | уточнить |
| `C24:WON` | **Фактический визит (успешная сделка)** | **meeting (conversion numerator)** |
| `C24:LOSE` | Отказ | closed |

The stage→reason/heat/bucket map is `_STAGE_SIGNALS` in `day.py`; the meeting
stage and category are settings (`companion_meeting_stage_id` = `C24:WON`,
`companion_tm_category_id` = 24).

**Which pipeline — and which NOT:** the scored telemarketers (dept 250) work their
live pipeline in **cat 24 "Zvandau"** (each has 28–137 open deals there). Two
lookalike pipelines are wrong for TM attribution: **cat 0 "Телемаркетинг"** is
legacy/closed-out (those managers have ~0 open deals there), and **cat 2 "Отдел
продаж"** belongs to the *sales closer*, not the TM (`cat 2 ASSIGNED_BY_ID` ≈ 0
for TMs). All three share similar stage names — distinguish by category id.

## Money axis (conversion → bonus)

- `meetings` = cat-24 deals that reached `C24:WON` ("Фактический визит") in the
  period (`>=CLOSEDATE`/`<CLOSEDATE`).
- `leads_processed` = cat-24 deals **created** in the period (`>=DATE_CREATE`/`<DATE_CREATE`).
- `conversion_pct` = meetings ÷ leads. `plan_pct` = meetings ÷ `companion_plan_target_meetings`
  (a Положение policy input, **not** in Bitrix). `crm_discipline_pct` = null until
  activity ingestion lands.

The `stats` counters (записаны-на-встречу / недозвоны / остывают, plus `no_task`)
are computed over the manager's **whole** open pipeline (up to
`companion_day_max_scan`), not just the shown action slice
(`companion_day_max_actions`), so the headline numbers are accurate even when the
action list is capped.

### «Без задачи» — брошенные карточки (`stats.no_task`)

An open deal with **no open (incomplete) activity** is a «брошенная» card without
a next step — the «Займись сейчас» queue *Без задачи*. `_deals_with_open_task()`
makes one extra `crm.activity.list` pass over the open deal ids
(`OWNER_TYPE_ID=2`, `COMPLETED='N'`, batched 50/call) and returns the set of
deals that *have* a task; the complement is the queue. This is **orthogonal to the
stage bucket**: a deal can be both `cooling` and `no_task`, so it is carried as a
separate boolean `DayActionItem.no_task` (not a `queue` value) and counted in its
own `DayStats.no_task`. `_select_action_deals` additionally surfaces *neutral-stage*
no-task deals (normally dropped) so the queue has examples. If the activity read
fails, `no_task` degrades to `null` (UI shows "—"), never a fake zero.

### Meeting attribution (solved 2026-06-12 — was misdiagnosed as a data gate)

A snapshot query (cat 24 + `STAGE_ID=C24:WON` + `ASSIGNED_BY_ID=TM`) **always
counts 0 by design**: the moment a visit becomes a fact, the deal is moved to
cat 2 «Отдел продаж» and reassigned to the sales closer, so it never *rests* at
the meeting stage. This read 0 meetings / 0% conversion for everyone and was
initially misread as the Bitrix data-cleanup gate ("конверсия недостоверна").

The portal in fact preserves both halves read-only:

- the conducted-meeting **fact** survives as a `crm.stagehistory.list` transition
  event (entityTypeId 2, `CATEGORY_ID=24`, `STAGE_ID=C24:WON`, `CREATED_TIME`);
- the **TM** survives on the deal in the «Сотрудник ТМ» employee field
  (`companion_tm_employee_field`, default `UF_CRM_1751599893`), which stays put
  after the reassignment — verified 100% filled on June 2026's 152 WON deals.

`_meetings_by_tm()` joins the two (distinct deals per TM, one shared pull per
period cached for `companion_day_cache_ttl_seconds`), and `_money()` reads its
manager's count from that map. Caveat: the field id dates it to ~July 2025, so
earlier months cannot be attributed this way — irrelevant for live bonus periods.

`DayView.data_ready` is False only when a manager has no open pipeline at all;
the companion then shows an honest "данные готовятся" state instead of zeros.
With this join in place, meetings=0 simply means no conducted visits in the
period.

## «Важные цифры дня» — the today block (`DayView.today`)

The Мой день card shows six **day-scoped** headline numbers (`DayToday`,
`_today_metrics` in `day.py`). The default day is today — `[midnight, midnight+1d)`
in the report timezone (`_today_window`) — but the endpoint accepts an optional
`?date=YYYY-MM-DD` so a manager can review a **past** day's results; `_day_window`
resolves it (`"today"` label by default, else the date, validated to a single day
via `okk.parse_period`). Only this block moves with `date`; the open-pipeline
queues/actions and the monthly money axis (`?period=`) stay **current** — they are
live pipeline state, not reconstructable per past day. The TTL cache key carries
the day label (`(uid, period_label, day_label)`). Each tile is resilient: if its
Bitrix read fails the field degrades to `null` (UI shows "—"), never a misleading
zero.

| Field | «Цифра» | Source (over the selected day) |
|---|---|---|
| `planned_calls` | записано на сегодня | `crm.activity.list` count: **open** (`COMPLETED=N`) call activities (`TYPE_ID=companion_call_activity_type_id`, default 2) with `DEADLINE` today, `RESPONSIBLE_ID=uid`. `COMPLETED=N` is essential — telephony auto-creates a *completed* call activity per real call, which would otherwise inflate the count to "planned + every call already made today" |
| `meetings_set` | назначено сегодня | distinct deals that **entered** `companion_meeting_set_stage_id` (`C24:EXECUTING`, «Записан на встречу») today, per `ASSIGNED_BY_ID` — pre-meeting stages still rest with the TM, so assignee is correct attribution (`_stage_entrants_by_assignee`) |
| `talk_time_sec` | время на линии | `voximplant.statistic.get` today, `PORTAL_USER_ID=uid`, summing `CALL_DURATION` over answered calls (`CALL_FAILED_CODE==ingest_success_code`) — full telephony, analyzed or not |
| `push_to_meeting` | дожать до встречи | distinct deals that **entered** any **hot** pre-booking stage (`_HOT_STAGES` — просил перезвонить / квалифицирован / не дошёл) today, per `ASSIGNED_BY_ID` — same `_stage_entrants_by_assignee`, a list of stages |
| `deals_closed` | дел закрыто | WON (`C24:WON`) transitions **today** attributed via «Сотрудник ТМ» — reuses `_meetings_by_tm` with the today window (same join as the money axis) |
| `overdue` | просроченных | `crm.activity.list` count: incomplete activities (`COMPLETED=N`) with `DEADLINE` in `[day 00:00, min(day end, now))` — due that day but already past deadline (for today this is `now`; a future day short-circuits to 0) (`_overdue_tasks`) |

Caching mirrors the money axis: `_stage_entrants_by_assignee` keeps its own
date-keyed cache (`_entrants_cache`, keyed by the stage set + window, so the
booking-stage and hot-stage pulls cache separately and each serves every
manager), and the WON-today count shares `_meetings_cache` with the period axis
(distinct cache key per date window). All honor `companion_day_cache_ttl_seconds`.

## Filter gotcha

Bitrix `crm.deal.list` date filters must be sent as a **JSON body** (the client's
default). A naive form-encoded `filter[>=DATE_CREATE]=...&filter[<DATE_CREATE]=...`
probe silently drops the lower bound (counts everything before the upper bound).
Always verify counts via the JSON transport the client uses.
