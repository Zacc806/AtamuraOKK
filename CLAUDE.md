# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A call-QA pipeline for a real-estate telemarketing team. It pulls **answered, recorded** calls from **Bitrix24** (Voximplant telephony), narrows them to the **analyzable** subset (a client's *first* call AND a *qualified* client — ~200 calls/day, not all calls), then **downloads → transcribes → scores against a QA rubric → reports**. Output lands in Postgres, is visualized in Metabase, and summarized in twice-daily Russian-language QA reports (.md/.docx) plus Telegram alerts.

The package name is intentionally CamelCase (`AtamuraOKK`); ruff's `N999` is disabled for it. Scoring/reporting/rubric code is intentionally in **Russian**, language-detection tables in **Kazakh** — ruff's RUF001/2/3 are disabled for those paths, so don't "fix" non-ASCII text there.

## Commands

All dev tasks go through the `Makefile` (run `make help` for the full list). Key ones:

```bash
make install          # uv sync (runtime + dev)
make up               # start Postgres via docker compose
make migrate          # alembic upgrade head
make lint             # ruff check AtamuraOKK tests
make fmt              # ruff format
make typecheck        # mypy --strict on AtamuraOKK
make test             # uv run pytest
```

Run a single test: `uv run pytest tests/test_echo.py::test_name -vv`. Tests require Postgres up (`make up`) and run with `ATAMURAOKK_ENVIRONMENT=pytest` against the `AtamuraOKK_test` DB (set via `pyproject.toml` pytest-env). pytest treats warnings as errors.

The pipeline stages each have a `python -m` CLI; the Makefile wraps them:

```bash
make ingest           # Bitrix -> Postgres (one incremental pull)
make ingest-download  # download analyzable recordings -> S3/MinIO
make ingest-requalify # promote first-calls whose client just became qualified
make transcribe       # DOWNLOADED -> TRANSCRIBED
make seed-rubric      # load active QA rubric into DB (run before scoring)
make score            # TRANSCRIBED -> SCORED (LLM)
make report-morning / report-afternoon   # generate a half-day report
make worker           # the unified always-on production worker (does all of the above on a schedule)
make ops-summary / ops-retry / ops-dead-letter   # observability + reliability
```

Each module CLI takes subcommands and `--limit`; see e.g. `python -m AtamuraOKK.ingestion --help`.

## Architecture

A linear, status-driven pipeline. Each call is a row in the `calls` table whose `status` enum (`AtamuraOKK/db/models/enums.py`) is the single source of truth for where it is:

```
NEW → DOWNLOADED → TRANSCRIBED → SCORED → (PUSHED)
       ↘ SKIPPED (out of analysis scope, with skip_reason)
       ↘ PENDING_KK (Kazakh call, parked — no Kazakh STT provider yet)
       ↘ FAILED (exhausted retries; see error column)
```

Each stage is a worker that **selects rows in the prior status and advances them**, idempotently. Stages never run in-process together except via the orchestrators. The flow is intentionally decoupled so any stage can be run/retried alone.

**Stages** (one package each under `AtamuraOKK/`):

- `ingestion/` — `service.run_ingestion()` pulls `voximplant.statistic.get` since a stored cursor (`IngestState`), upserts on `bitrix_call_id` (never clobbering pipeline-owned columns), attributes each call to a `Manager`, and computes **analysis scope**. Scope = `is_first_call AND client_qualified`; non-analyzable calls go to `SKIPPED` with a reason. Qualification is checked via deal stage-history (`qualification.py`) because clients qualify *after* the first call, so `refresh_qualification()` periodically promotes `SKIPPED(not_qualified) → NEW`. `download.py` is a separate stage that fetches recordings to object storage.
- `transcription/` — splits stereo recordings into agent/customer channels (`AtamuraOKK/audio.py`, ffmpeg), transcribes each, stores a speaker-labeled `Transcript`, detects language (`language.py`). Kazakh → `PENDING_KK`.
- `scoring/` — runs the active rubric (`rubric.py` / seeded `RubricVersion`) through an LLM that returns the **structured** `CallScore` (`base.py`) via forced tool-use; the worker derives numeric total/percent/zone. Only genuine `квалификация` (qualification) calls count toward the team score; reminders/vendor/internal calls are classified out.
- `reporting/` — `aggregate.py` + `render.py` produce the twice-daily Russian QA report (`writer.py` → .md/.docx in `reports/`).
- `ops/` — `retry.py` (requeue FAILED under `max_retries`, else dead-letter), `summary.py` (daily run-summary), `alert.py` (Telegram alerter; logs if no token).

**Provider abstraction**: transcription and scoring are vendor-agnostic. The worker depends only on an interface (`AsyncTranscriber` in `transcription/base.py`, `Scorer` in `scoring/base.py`) and a `factory.py` picks the concrete impl from settings. Current defaults (see memory): **transcription = local faster-whisper large-v3** (`transcribe_provider="whisper"`, no API quota), **scoring = Anthropic Claude Sonnet** (`scoring_provider="anthropic"`). OpenAI impls exist as alternates. When touching scoring, use the latest Claude models and the `anthropic` SDK with forced tool-use for structured output.

**Orchestration**: `AtamuraOKK/worker.py` is the production entry point — one long-lived APScheduler process running the whole pipeline (pipeline pass every N hours, retry pass, two reports, daily summary), each job `max_instances=1, coalesce=True` and self-guarding so one failure can't take down the scheduler. This replaces running the per-stage CLIs as separate cron jobs.

**Web app**: `AtamuraOKK/web/` is a FastAPI app (from `fastapi_template`) with health/monitoring + echo endpoints and Swagger at `/api/docs`. It is *not* the pipeline driver — the pipeline runs via the module CLIs and `worker.py`.

**Data layer**: async SQLAlchemy 2.0 + asyncpg, sessions via `db/session.py:session_scope()`. Models in `db/models/`; `load_all_models()` auto-imports them for Alembic. Migrations in `db/migrations/versions/` (`make migrate` / `alembic revision --autogenerate`).

**Infra** (`docker-compose.yml`): `db` (Postgres), `minio` (S3-compatible object storage for recordings), `worker` (the orchestrator), `metabase` (dashboards), `migrator`. `metabase/bootstrap.py` + `provision_dashboards.py` provision the BI layer over the API.

## Configuration

All settings live in `AtamuraOKK/settings.py` (`pydantic-settings`). Every env var is prefixed `ATAMURAOKK_` and read from `.env` (e.g. `ingest_min_duration_sec` → `ATAMURAOKK_INGEST_MIN_DURATION_SEC`). Bitrix webhook also accepts the bare `BITRIX_WEBHOOK`. Notable knobs: `transcribe_provider` / `scoring_provider`, `qualified_stage_name` (the Kanban column that marks a qualified client; stage IDs auto-discovered), `ingest_require_qualified`, `report_timezone` (Asia/Qyzylorda), `whisper_device`/`whisper_compute_type` (CPU int8 is the fast path on Apple Silicon — CTranslate2 has no Metal backend).

## Conventions

- Python 3.12+, ruff (line length 88, large ruleset) + mypy `--strict`; both run in pre-commit (`pre-commit install`). Heavy/optional spike deps are in the `spike` dependency group (`uv sync --group spike`).
- Stage workers expose a pure async function (`run_ingestion`, `transcribe_pending`, `score_pending`, …) that the CLI, `worker.py`, and tests all call — keep that function the unit of work, not the CLI.
- `AtamuraOKK/spike/` is the Phase-0 transcription/WER evaluation harness; not part of the production path.

There is no README-documented "common tasks" list beyond the above; deeper per-stage docs live in `docs/` (`ingestion.md`, `transcription.md`, `scoring.md`, `dashboard.md`, `reports.md`, `operations.md`, `deployment.md`).
Use comments sparingly. Only comment complex code.