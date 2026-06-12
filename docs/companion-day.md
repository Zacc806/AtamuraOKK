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

The three `stats` counters (записаны-на-встречу / недозвоны / остывают) are
computed over the manager's **whole** open pipeline (up to `companion_day_max_scan`),
not just the shown action slice (`companion_day_max_actions`), so the headline
numbers are accurate even when the action list is capped.

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

## Filter gotcha

Bitrix `crm.deal.list` date filters must be sent as a **JSON body** (the client's
default). A naive form-encoded `filter[>=DATE_CREATE]=...&filter[<DATE_CREATE]=...`
probe silently drops the lower bound (counts everything before the upper bound).
Always verify counts via the JSON transport the client uses.
