.PHONY: help install install-spike lint fmt typecheck test up down migrate \
        spike-fetch spike-download spike-transcribe spike-wer

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install runtime + dev dependencies
	uv sync

install-spike: ## Install heavy Phase 0 spike deps (faster-whisper, jiwer, soundfile)
	uv sync --group spike

lint: ## Ruff lint
	uv run ruff check AtamuraOKK tests

fmt: ## Ruff format
	uv run ruff format AtamuraOKK tests

typecheck: ## mypy strict
	uv run mypy AtamuraOKK

test: ## Run the test suite
	uv run pytest

up: ## Start Postgres (+ app) via docker compose
	docker compose up -d db

down: ## Stop docker compose stack
	docker compose down

migrate: ## Apply DB migrations
	uv run alembic upgrade head

# --- Phase 1 ingestion ---
ingest: ## One incremental pull (Bitrix -> Postgres)
	uv run python -m AtamuraOKK.ingestion ingest

ingest-download: ## Download analyzable recordings -> object storage
	uv run python -m AtamuraOKK.ingestion download

ingest-requalify: ## Re-check pending first-calls; promote newly-qualified
	uv run python -m AtamuraOKK.ingestion requalify

# --- Phase 2 transcription ---
transcribe: ## Transcribe analyzable DOWNLOADED calls (OpenAI gpt-4o-transcribe)
	uv run python -m AtamuraOKK.transcription run

# --- Phase 3 scoring ---
seed-rubric: ## Load the active QA rubric into the DB
	uv run python -m AtamuraOKK.scoring seed

score: ## Score analyzable TRANSCRIBED calls (LLM, conversational rubric)
	uv run python -m AtamuraOKK.scoring run

# --- Phase 4 dashboard (Metabase) ---
metabase-up: ## Start the Metabase container
	docker compose up -d metabase

metabase-bootstrap: ## Provision Metabase admin + Postgres data source (set METABASE_ADMIN_PASSWORD)
	uv run python metabase/bootstrap.py

# --- Phase 5 reports ---
report-morning: ## Generate the first-half (morning) report for today
	uv run python -m AtamuraOKK.reporting generate --half morning --run-pipeline

report-afternoon: ## Generate the second-half (afternoon) report for today
	uv run python -m AtamuraOKK.reporting generate --half afternoon --run-pipeline

report-schedule: ## Run reports twice daily (lunch=first half, evening=second half)
	uv run python -m AtamuraOKK.reporting schedule

# --- Phase 5 ops (observability + reliability) ---
ops-summary: ## Print today's run-summary (add --send for Telegram)
	uv run python -m AtamuraOKK.ops summary

ops-retry: ## Requeue FAILED calls (under the retry cap) for another attempt
	uv run python -m AtamuraOKK.ops retry

ops-dead-letter: ## List FAILED calls that exhausted retries
	uv run python -m AtamuraOKK.ops dead-letter

ingest-run: ## Ingest then download (one full pass)
	uv run python -m AtamuraOKK.ingestion run

ingest-schedule: ## Run now, then every N hours (default 3)
	uv run python -m AtamuraOKK.ingestion schedule

# --- Phase 0 transcription spike ---
spike-fetch: ## Pull recent answered+recorded calls (telephony scope)
	uv run python -m AtamuraOKK.spike fetch

spike-download: ## Download recordings (needs disk scope for external-integration calls)
	uv run python -m AtamuraOKK.spike download

spike-transcribe: ## Transcribe with faster-whisper (needs spike group + ffmpeg)
	# CPU int8 is the fast path on Apple Silicon (CTranslate2 has no Metal backend).
	ATAMURAOKK_WHISPER_DEVICE=cpu ATAMURAOKK_WHISPER_COMPUTE_TYPE=int8 \
		uv run python -m AtamuraOKK.spike transcribe

spike-wer: ## Compute WER vs hand-corrected references
	uv run python -m AtamuraOKK.spike wer
