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
make worker           # the legacy single-process APScheduler worker (does all of the above on a schedule)
make ops-summary / ops-retry / ops-dead-letter   # observability + reliability
```

The production default is the **distributed dispatch** layer (arq + Redis), which has no Makefile target — run roles directly with `python -m AtamuraOKK.dispatch <dispatcher|download|transcribe|score>` or via `docker compose up` (see **Orchestration**). `make worker` is the legacy fallback.

Each module CLI takes subcommands and `--limit`; see e.g. `python -m AtamuraOKK.ingestion --help`.

## Architecture

A linear, status-driven pipeline. Each call is a row in the `calls` table whose `status` enum (`AtamuraOKK/db/models/enums.py`) is the single source of truth for where it is:

```
NEW → DOWNLOADING → DOWNLOADED → TRANSCRIBING → TRANSCRIBED → SCORING → SCORED → (PUSHED)
       ↘ SKIPPED (out of analysis scope, with skip_reason)
       ↘ PENDING_KK (Kazakh call, parked — only when the STT engine can't do Kazakh)
       ↘ FAILED (exhausted retries; see error column)
```

The `*ING` states are **in-flight claims**: a worker atomically flips a ready row into one (e.g. `NEW → DOWNLOADING`) so no other worker re-processes it. The `claimed_at` column + a per-stage TTL let the reconciler revert a crashed worker's claim. Under the legacy single-process worker these states are momentary; under the distributed dispatcher they are how concurrency stays race-safe (see **Orchestration**).

Each stage is a worker that **selects rows in the prior status and advances them**, idempotently. The per-stage unit of work comes in two granularities: a batch function (`download_pending`/`transcribe_pending`/`score_pending` — select-and-advance a batch) and a single-call function (`download_one`/`transcribe_one`/`score_one(call_id, …)` — advance one already-claimed row, re-checking the claim so a duplicate delivery returns `"skipped"`). The CLI/legacy worker use the batch form; the broker tasks use the single-call form. Stages never run in-process together except via the orchestrators, so any stage can be run/retried alone.

**Stages** (one package each under `AtamuraOKK/`):

- `ingestion/` — `service.run_ingestion()` pulls `voximplant.statistic.get` since a stored cursor (`IngestState`), upserts on `bitrix_call_id` (never clobbering pipeline-owned columns), attributes each call to a `Manager`, and computes **analysis scope**. Scope = `is_first_call AND client_qualified`; non-analyzable calls go to `SKIPPED` with a reason. Qualification is checked via deal stage-history (`qualification.py`) because clients qualify *after* the first call, so `refresh_qualification()` periodically promotes `SKIPPED(not_qualified) → NEW`. `download.py` is a separate stage that fetches recordings to object storage.
- `transcription/` — splits stereo recordings into agent/customer channels (`AtamuraOKK/audio.py`, ffmpeg), transcribes each, stores a speaker-labeled `Transcript`, detects language (`language.py`). Kazakh advances to `TRANSCRIBED` when the engine can do Kazakh (SpeechKit), else parks at `PENDING_KK` (gated per-provider by a `handles_kazakh` flag; `requeue-kk` re-queues parked calls).
- `scoring/` — runs the active rubric (`rubric.py` / seeded `RubricVersion`) through an LLM that returns the **structured** `CallScore` (`base.py`) via forced tool-use; the worker derives numeric total/percent/zone. Only genuine `квалификация` (qualification) calls count toward the team score; reminders/vendor/internal calls are classified out.
- `reporting/` — `aggregate.py` + `render.py` produce the twice-daily Russian QA report (`writer.py` → .md/.docx in `reports/`).
- `ops/` — `retry.py` (requeue FAILED under `max_retries`, else dead-letter), `summary.py` (daily run-summary), `alert.py` (Telegram alerter; logs if no token).

**ОП meeting pipeline** (`scoring/meetings/`): a parallel, mostly self-contained pipeline that scores **meeting recordings** (отдел продаж) from the "Встречи ОП" Bitrix Disk folder — own config (`meetings/config.py`, same `.env`), own SQLite working state (`.meetings/meetings.db`, statuses NEW → DOWNLOADED → TRANSCRIBED → SCORED), own scheduler (`python -m AtamuraOKK.scoring.meetings.worker`; deploy units in `deploy/`). CLI: `python -m AtamuraOKK.scoring.meetings <ingest|download|transcribe|score|push|run|drain|retry|rescore|report|status>`. The one seam into the call pipeline's Postgres is the **push stage** (`meetings/push.py`): SCORED rows are upserted into the `meetings` table (idempotent on `bitrix_file_id`, `pushed_at` in SQLite marks done), attributed to the Disk uploader (`CREATED_BY` → get-or-created `managers` row), tagged with a `source` ("op"; more departments later) — that table feeds the companion API's `/managers/{id}/meetings` + `/meetings/{id}/feedback`, the unified `/managers/{id}/feed`, and the meetings blocks in scorecard/team summary. `source` is also the rubric axis: `make seed-rubric` seeds one active rubric per source into `rubric_versions` ("tm" = call rubric, "op" = the meeting rubric, keyed by its file-stem id), which `GET /api/v1/rubrics` serves — each department scores against its own criteria. Postgres being down only delays the push; all other meeting stages run without it.

**Provider abstraction**: transcription and scoring are vendor-agnostic. The worker depends only on an interface (`AsyncTranscriber` in `transcription/base.py`, `Scorer` in `scoring/base.py`) and a `factory.py` picks the concrete impl from settings. Current defaults (see memory): **transcription = Yandex SpeechKit v3** (`transcribe_provider="yandex"`, Russian + Kazakh, streaming gRPC, IAM-token auth from a service-account authorized-key JSON; see `docs/transcription.md` for the region/endpoint/billing gotchas), **scoring = Anthropic Claude Sonnet** (`scoring_provider="anthropic"`). Local faster-whisper (`"whisper"`, no API quota) and OpenAI impls remain selectable alternates. When touching scoring, use the latest Claude models and the `anthropic` SDK with forced tool-use for structured output.

**Orchestration** — two interchangeable models over the same Postgres-as-source-of-truth pipeline:

- *Legacy single process* (`AtamuraOKK/worker.py`): one long-lived APScheduler process running the whole pipeline (pipeline pass every N hours, retry pass, two reports, daily summary), each job `max_instances=1, coalesce=True` and self-guarding so one failure can't take down the scheduler. In compose it's now the `legacy` profile (`docker compose --profile legacy up worker`) — a no-Redis fallback for local dev.
- *Distributed dispatch* (`AtamuraOKK/dispatch/`, the production default): an **arq + Redis** broker fan-out. One singleton **dispatcher** beat (`dispatcher.py`) ticks every `dispatch_interval_seconds`: it reconciles stale claims, runs the *singleton* ingestion + requalification pass (they share one cursor, never fanned out), then `claim.claim_ready()` atomically claims ready rows per stage via `SELECT … FOR UPDATE SKIP LOCKED` and enqueues one task per call. Per-stage **worker pools** (`download`/`transcribe`/`score`, each its own queue and process so the CPU-bound transcribe pool scales independently) consume those tasks, preloading expensive resources (whisper model, scorer, rubric) once in `on_startup`. Redis is **transient** (cache-only, no persistence): the claim in Postgres is what prevents double-processing, so a wiped Redis just means the next tick re-enqueues everything still in a ready status. Reports/retry/daily-summary stay singleton cron jobs on the dispatcher. Run a role with `python -m AtamuraOKK.dispatch <dispatcher|download|transcribe|score>`; scale a stage with `docker compose up --scale score=3`. The `broker` dependency group (`uv sync --group broker`) is required and imported only by `dispatch/` — the stages and `worker.py` never pull in arq.

**Web app**: `AtamuraOKK/web/` is a FastAPI app (from `fastapi_template`) with health/monitoring + echo endpoints, Swagger at `/api/docs`, and the companion read API under `/api/v1` (see `docs/companion-api.md`). The companion API has two auth layers: the shared service bearer (nginx-injected by the sales-companion BFF) plus a personal `X-Companion-User-Key` — either the **static РОП key** (`ATAMURAOKK_COMPANION_HEAD_KEY`, grants `head` with no DB row) or a `companion_users` row whose role scopes access — `manager` sees only their own data, `head` (РОП) sees everything. The head issues/revokes **manager** keys from the cabinet (`/api/v1/users`, the API's only writable surface — it can never mint a head); the CLI `python -m AtamuraOKK.companion_users` remains for everything else. It is *not* the pipeline driver — the pipeline runs via the module CLIs and `worker.py`.

**Data layer**: async SQLAlchemy 2.0 + asyncpg, sessions via `db/session.py:session_scope()`. Models in `db/models/`; `load_all_models()` auto-imports them for Alembic. Migrations in `db/migrations/versions/` (`make migrate` / `alembic revision --autogenerate`).

**Infra** (`docker-compose.yml`): `db` (Postgres), `minio` (S3-compatible object storage for recordings), `redis` (transient broker), the dispatch services (`dispatcher` + `download`/`transcribe`/`score` stage workers, sharing a `worker_base` anchor), `worker` (legacy all-in-one, `legacy` profile), `metabase` (dashboards), `migrator`. `metabase/bootstrap.py` + `provision_dashboards.py` provision the BI layer over the API.

## Configuration

All settings live in `AtamuraOKK/settings.py` (`pydantic-settings`). Every env var is prefixed `ATAMURAOKK_` and read from `.env` (e.g. `ingest_min_duration_sec` → `ATAMURAOKK_INGEST_MIN_DURATION_SEC`). Bitrix webhook also accepts the bare `BITRIX_WEBHOOK`. Notable knobs: `transcribe_provider` / `scoring_provider`, `qualified_stage_name` (the Kanban column that marks a qualified client; stage IDs auto-discovered), `ingest_require_qualified`, `report_timezone` (Asia/Qyzylorda), `whisper_device`/`whisper_compute_type` (CPU int8 is the fast path on Apple Silicon — CTranslate2 has no Metal backend). Distributed-worker knobs: `redis_url`, `dispatch_interval_seconds` (arq cron is calendar-based, so a sub-minute cadence must divide 60 — else it falls back to once/minute), `claim_batch_size`, per-stage `download_concurrency`/`score_concurrency` (transcribe reuses `transcribe_concurrency`) and `claim_stale_seconds_*` TTLs, plus `db_pool_size`/`db_max_overflow` (keep `(dispatcher + stage replicas) * (pool_size + max_overflow)` under Postgres `max_connections`).

## Conventions

- Python 3.12+, ruff (line length 88, large ruleset) + mypy `--strict`; both run in pre-commit (`pre-commit install`). Heavy/optional spike deps are in the `spike` dependency group (`uv sync --group spike`).
- Stage workers expose a pure async function (`run_ingestion`, `transcribe_pending`, `score_pending`, …) that the CLI, `worker.py`, and tests all call — keep that function the unit of work, not the CLI.
- `AtamuraOKK/spike/` is the Phase-0 transcription/WER evaluation harness; not part of the production path.

There is no README-documented "common tasks" list beyond the above; deeper per-stage docs live in `docs/` (`ingestion.md`, `transcription.md`, `scoring.md`, `dashboard.md`, `reports.md`, `operations.md`, `deployment.md`).
Use comments sparingly. Only comment complex code.