# Phase 5 — Auto-generated QA reports (twice daily)

Generates the ОКК telemarketing report in the company's style, **twice a day**:
- **Lunch** run → report for the **first half** of the day (00:00–13:00).
- **Evening** run → report for the **second half** (13:00–19:00).

Each report is written to `reports/<date>_<half>.{md,docx}` (gitignored).

## What's in a report
Mirrors `docs/Новый документ.docx`:
- Header + **general stats** (calls scored, team avg % vs 75 norm, zone mix,
  target/non-target, flagged count).
- **Overall assessment** (LLM).
- **Manager ranking grouped by zone** (85+/80–84/75–79/<75), each with
  LLM-synthesized **strengths / growth-zone / training** from their calls.
- **Systemic errors** (LLM), **weakest-criteria** table (data), **flagged calls**,
  **tasks for РОПы** (LLM), and a **conclusion**.

## Pipeline freshness
Scheduled runs use `--run-pipeline`: ingest → requalify → download → transcribe →
score the latest calls **before** building the report, so each half-day report
reflects that half's calls.

## Run it
```bash
# one-off (today):
make report-morning           # or: report-afternoon
uv run python -m AtamuraOKK.reporting generate --half morning --date 2026-05-28

# twice-daily scheduler (long-running process; APScheduler):
make report-schedule
```

### Production scheduling — two options
- **A. Long-running scheduler** (`make report-schedule`) under systemd / a tmux
  session / a docker service. Fires at `report_lunch_hour` and
  `report_evening_hour` in `report_timezone`.
- **B. System cron** (no daemon to keep alive):
  ```cron
  0 13 * * *  cd /path/AtamuraOKK && make report-morning   >> reports/cron.log 2>&1
  0 19 * * *  cd /path/AtamuraOKK && make report-afternoon >> reports/cron.log 2>&1
  ```

## Config (settings / .env)
`report_timezone` (default `Asia/Qyzylorda`), `report_split_hour` (13),
`report_day_end_hour` (19), `report_lunch_hour` (13), `report_evening_hour` (19),
`report_model` (gpt-4o), `report_dir` (`./reports`), `report_score_norm` (75).

## Components
- `reporting/aggregate.py` — window → structured data (from the reporting views).
- `reporting/writer.py` — LLM Structured-Outputs narrative.
- `reporting/render.py` — Markdown + `.docx`.
- `reporting/worker.py` — `generate_report(half, day, run_pipeline)`.
- `reporting/__main__.py` — `generate` / `schedule` CLI.

## Validated
Generated `2026-05-28_morning` over 13 real scored calls: valid `.md` + `.docx`,
team avg 28.9%, 12/13 in risk, systemic errors (weak closing / needs / programming)
matching the real reports, weakest-criteria table, flagged queue, and РОП tasks.
