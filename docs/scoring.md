# Phase 3 — Analysis & Scoring

Turns each Russian transcript into the ОКК's structured QA assessment.

```
TRANSCRIBED call → load transcript → LLM scores vs active rubric (Structured Outputs)
  → enforce rubric maxima + "full marks if no objections"
  → derive raw points → percent (over 91) → zone
  → persist Score (per-criterion + blocks + sentiment + summary + flags + target
     + strengths/growth/training) → status SCORED
```

## Rubric (`tm-call-v2` — 5 holistic criteria)
`AtamuraOKK/scoring/rubrics/tm_call_v2.json` — the active rubric collapses the old
20-line checklist into **5 big criteria**, each scored holistically over its own
0..max scale, weights = the sum of that block's former line-items:

| # | Criterion (`block_id`) | Max |
|---|------------------------|-----|
| 1 | Приветствие (`greeting`) | 5 |
| 2 | Выявление потребности (`needs`) | 17 |
| 3 | Презентация (`presentation`) | 11 |
| 4 | Закрытие на КЭВ (`closing`) — folds in the old «дожим» item | 37 |
| 5 | Отработка возражений (`objections`) — full marks if no objection | 21 |

Total **91**. The 3 CRM/WhatsApp items (WhatsApp send, CRM-card data, tasks/касания)
are **not audio-scorable** and stay out of the rubric (the parked 9 pts to 100).
Final metric = points / 91 × 100. Zones: **85+ strong / 80–84 normal / 75–79
borderline / <75 risk**. The prior `tm_call_v1.json` (21-item) is kept for history;
re-score calls (`make score` / re-run the score stage) to move them onto v2.

## Call-type classification (avoids polluting the metric)
Not every answered+recorded "first call" is a qualification call — only a genuine
conversation with a potential **buyer** is. The scorer classifies `call_type`
(квалификация / напоминание / нецелевое_обращение / вендор_или_спам / внутренний
/ недозвон_или_ошибка / …) and sets **`is_qualification_call`**. Only qualification
calls count toward team scores (and as an "attempt to book into ОП") — reminders,
**non-client inquiries** (`нецелевое_обращение`: realtor/agent, job applicant
(résumé), partner, complaint, …), vendor/spam/КП, internal (e.g. headset tests),
and wrong-numbers are scored but **excluded** from averages/zones in the reports
and dashboards (the report summarizes their count by `call_type`). The scorer also
returns `manager_identified`.

Speaker labels are presented to the model **by audio channel, not role** (the
Atamura manager is often on either channel), and the prompt has it identify the
manager from content — fixing a class of mislabeled-speaker mis-scores.

> The objection block (21 of 91 pts) is awarded in full when no objection occurs,
> so a call's score can swing ~23 pts on whether an objection was present — this is
> inherent to the company checklist, not a bug.

## What the scorer returns (per call)
Per-criterion `{score, max, justification, evidence-quote, recommendation}` (the
recommendation = Claude's concrete "improve this next call" feedback per criterion);
block subtotals; total
% + zone; **target/non-target**; customer & agent **sentiment**; 2–3 sentence
**summary**; **red flags**; and the **strengths / growth-zone / training
recommendation** the reports need — all in Russian.

## Components
- `scoring/rubric.py` — load the versioned rubric + zone/percent helpers.
- `scoring/base.py` — `Scorer` interface + `CallScore` (validated schema).
- `scoring/prompt.py` — bilingual (RU/KK) prompt; instructs the model to identify
  the *manager* regardless of channel labels and score only them.
- `scoring/openai_scorer.py` — OpenAI **Structured Outputs** (`gpt-4o`, temp 0).
- `scoring/worker.py` — `score_pending`: TRANSCRIBED → SCORED, applies rubric math.
- `scoring/seed.py` — seed the active rubrics into `rubric_versions`: the call
  rubric under `source="tm"` **and** the ОП meeting rubric under `source="op"`
  (one active row per source — departments score against their own criteria;
  the companion `GET /api/v1/rubrics` reads these rows).

## Run
```bash
make seed-rubric     # load both active rubrics into the DB (once / on change)
make score           # score analyzable TRANSCRIBED calls (today only by default)
uv run python -m AtamuraOKK.scoring run --all   # also score the older backlog
```
Requires `ATAMURAOKK_OPENAI_API_KEY`.

## Today-only auto-scoring
`score_auto_today_only` (default **True**) restricts **automatic** scoring — the
distributed dispatcher and the legacy worker — to calls whose `started_at` is on
the current day (report timezone). Older `TRANSCRIBED` calls accumulate untouched
and are scored only **on demand** via `python -m AtamuraOKK.scoring run --all`
(`score_pending(since=None)` / `claim_ready(..., since=None)`). This caps the daily
LLM spend to the day's fresh calls and stops a recovered backlog (e.g. after an
Anthropic-credit outage requeues thousands of FAILED rows back to `TRANSCRIBED`)
from auto-draining credits. Set `ATAMURAOKK_SCORE_AUTO_TODAY_ONLY=false` to
auto-score the full backlog again.

## Validated live (13 Russian calls)
Scores ranged **1–84%** with sound, well-calibrated judgments: a tile-factory
vendor cold-call and "wrong number" calls scored ~0–1% (flagged non-target); an
engaged qualification+presentation call scored 83.5%. The no-objection full-marks
rule and manager identification both work; mono and stereo transcripts both handled.

> Note: scoring *first* calls skews low — many first touches are short qualification
> attempts that fail immediately. The ОКК's ~72% average reflects curated
> substantive calls, not raw first-touches.

## Reporting views (Phase 4 groundwork)
Two DB views flatten the latest score per call out of JSONB for Metabase, with
**latest-score-wins** (`DISTINCT ON (call_id) ORDER BY created_at DESC`) so
distributions stay correct after re-scoring:
- **`call_scores_latest`** — one row per call: `percent, zone, target_status,
  sentiment_*, manager_name, department_name, direction, started_at, summary,
  strengths/growth/training, red_flags`. Drives scorecards, zone roll-ups,
  target/non-target, trends.
- **`call_criteria_latest`** — one row per (call, criterion): `score, max,
  percent_of_max, block, justification, evidence, recommendation`. Drives
  per-criterion/block distributions and "team's weakest criteria".

(Migration `b2c3d4e5f6a7`. Views aren't autogen-tracked, so `alembic check` stays
clean.)

## Per-criterion appeals (cabinet-only correction)
A manager can contest specific criteria of a call's score; the head listens to
the recording and confirms the ones the manager was right about. Each confirmed
criterion is **awarded full marks** and the call's percent **recalculates
automatically** from the stored `per_criterion` breakdown
(`AtamuraOKK/scoring/recompute.py:recompute_percent`, the same numerator/
denominator math as `worker._assemble`). The corrected percent is stored on the
appeal and preferred over the LLM percent in the **companion read layer only** —
the `call_scores_latest` view and the twice-daily QA reports keep the model's
original verdict for audit. See `docs/companion-api.md` for the API surface.

**Known limitation:** the corrected percent is computed and stored at review
time, so re-scoring a call (a fresh LLM run) after an appeal was accepted won't
auto-update an existing override — same behaviour as any stored correction.

## Not yet scored (deferred)
The 9 CRM/WhatsApp points (#13/#19/#20). Add later via Wazzup (WhatsApp) + Bitrix
(deal fields, tasks/activities) for full 100-point parity.
