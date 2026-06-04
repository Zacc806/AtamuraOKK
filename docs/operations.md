# Phase 5 — Operations (observability & reliability)

## Daily run-summary
```bash
make ops-summary                 # today (report timezone)
uv run python -m AtamuraOKK.ops summary --date 2026-05-28 --send   # specific day + Telegram
```
Reports, for the day: calls **ingested / transcribed / scored**, the **backlog**
(awaiting download / transcription / scoring), **failures** by stage + dead-letter
count, **Kazakh parked**, **audio minutes** (stereo billed as 2 channels), and an
**estimated cost** (transcription audio-minutes × rate + scoring tokens × rate).
Cost rates are configurable (`cost_*` settings).

Example:
```
📊 Atamura QA — сводка за 2026-06-04
Обработано: ингест 237, транскрибировано 13, оценено 26 (KK отложено: 2)
Очередь: загрузка 158, транскрипция 0, оценка 0
Ошибки: всего FAILED 0 (—); dead-letter 0
Аудио: 31.5 мин; оценка стоимости ~$0.64 (транскрипция $0.19 + оценка $0.45)
```

## Reliability: retries & dead-letter
Every stage increments `attempts` and sets `FAILED` on error. Recovery:
```bash
make ops-retry          # requeue FAILED calls (attempts < max_retries)
make ops-dead-letter    # list calls that exhausted retries (manual review)
```
`retry` routes each FAILED call back to the right stage by what artifacts exist:
transcript → re-score (`TRANSCRIBED`); audio only → re-transcribe (`DOWNLOADED`);
neither → re-download (`NEW`). Calls at/over `max_retries` (default 4) are the
**dead-letter** queue (left `FAILED`).

`requeue_failed()` also runs **automatically** at the start of every report
pipeline pre-pass, so transient failures self-heal each cycle.

## Alerts
Telegram when configured, otherwise logged. Set `telegram_bot_token` +
`telegram_chat_id`. `ops retry` alerts when ≥ `alert_failure_threshold` calls hit
dead-letter; `ops summary --send` pushes the daily summary.

## Suggested cron (production)
```cron
# hourly auto-recovery
0 * * * *   cd /path/AtamuraOKK && make ops-retry        >> reports/ops.log 2>&1
# daily summary to Telegram (19:30 local)
30 19 * * * cd /path/AtamuraOKK && uv run python -m AtamuraOKK.ops summary --send
```

## Settings
`max_retries` (4), `cost_transcribe_per_min` (0.006), `cost_score_input_per_1k`
(0.0025), `cost_score_output_per_1k` (0.01), `telegram_bot_token`,
`telegram_chat_id`, `alert_failure_threshold` (5).

## Components
- `ops/summary.py` — daily aggregation + render.
- `ops/retry.py` — `requeue_failed` + `dead_letter`.
- `ops/alert.py` — `Alerter` (Telegram / log fallback).
- `ops/__main__.py` — `summary` / `retry` / `dead-letter` CLI.
