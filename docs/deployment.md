# Production deployment

The whole pipeline runs unattended as **one always-on worker process** plus the
supporting containers (Postgres, MinIO, Metabase). No external cron is required —
the worker owns its own schedule.

## What the worker does (`python -m AtamuraOKK.worker`)
A single long-lived process running one APScheduler instance with these jobs:

| Job | Schedule (defaults) | Action |
|-----|--------------------|--------|
| `pipeline` | every `worker_ingest_interval_hours` (3h) | ingest → requalify → download → transcribe → score |
| `retry` | every `worker_retry_interval_hours` (1h) | requeue FAILED calls (auto-recovery) |
| `report-morning` | `report_lunch_hour`:00 (13:00) | first-half QA report (runs pipeline first) |
| `report-afternoon` | `report_evening_hour`:00 (19:00) | second-half QA report (runs pipeline first) |
| `daily-summary` | `report_day_end_hour`:30 (19:30) | build + send the daily run-summary |

All times are in `report_timezone` (`Asia/Qyzylorda`). Every job uses
`max_instances=1` + `coalesce=True` (a slow run never overlaps itself) and catches
its own exceptions, so one failure can't take the scheduler down. On startup it
runs one retry + pipeline pass immediately (`worker_run_on_start`).

## Run it with Docker (recommended)
The `worker` service is in `docker-compose.yml`. Bring up the full stack:
```bash
docker compose up -d db migrator minio worker metabase
```
The worker shares the app image (built once), depends on a healthy DB + MinIO and
the completed migrator, restarts on failure, and writes reports to `./reports` on
the host. It reads secrets from `.env` (Bitrix webhook, OpenAI key, S3, Telegram);
DB and MinIO hosts are overridden to the compose network names automatically.

```bash
make worker-up      # docker compose up -d worker
make worker-logs    # tail the worker logs
```

## Run it without Docker
```bash
make worker         # uv run python -m AtamuraOKK.worker
```
Use a process supervisor so it restarts on crash/reboot — e.g. a systemd unit:
```ini
# /etc/systemd/system/atamura-worker.service
[Unit]
Description=Atamura QA pipeline worker
After=network-online.target

[Service]
WorkingDirectory=/opt/AtamuraOKK
EnvironmentFile=/opt/AtamuraOKK/.env
ExecStart=/usr/bin/uv run python -m AtamuraOKK.worker
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
```bash
systemctl enable --now atamura-worker
journalctl -u atamura-worker -f
```

## Requirements
- **ffmpeg** in the runtime (stereo channel split for transcription). The Docker
  image installs it; on a bare VM `apt-get install ffmpeg`.
- Reachable Postgres, MinIO/S3, Bitrix webhook, OpenAI API, and (optionally)
  Telegram for alerts/summary.

## Settings
`worker_ingest_interval_hours` (3), `worker_retry_interval_hours` (1),
`worker_run_on_start` (true), `worker_send_daily_summary` (true). Report/timezone
settings (`report_timezone`, `report_lunch_hour`, `report_evening_hour`,
`report_day_end_hour`) drive the report and summary jobs — see
[reports.md](reports.md) and [operations.md](operations.md).

## Relationship to the manual commands
The worker just schedules the same code paths the `make` targets run by hand
(`make ingest-run`, `make transcribe`, `make score`, `make report-*`,
`make ops-retry`, `make ops-summary`). Run those ad-hoc; run the worker for 24/7
operation. Don't run both `make worker` and `make *-schedule` at once — they'd
duplicate work.
