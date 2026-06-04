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

ingest-run: ## Ingest then download (one full pass)
	uv run python -m AtamuraOKK.ingestion run

ingest-schedule: ## Run now, then every N hours (default 3)
	uv run python -m AtamuraOKK.ingestion schedule

# --- Phase 2/3 transcription + scoring ---
transcribe: ## Transcribe DOWNLOADED calls (OpenAI ru / Yandex kk) -> TRANSCRIBED
	uv run python -m AtamuraOKK.transcription

score: ## Score TRANSCRIBED calls (Anthropic, tm_call_v3) -> SCORED
	uv run python -m AtamuraOKK.scoring

score-meetings: ## Score TRANSCRIBED ОП meetings (Anthropic, okk_meeting_v1) -> SCORED
	uv run python -m AtamuraOKK.scoring --kind meeting

calibrate-meetings: ## Calibration gate: AI meeting scores vs human OKK xlsx (PASS/REVISE/FAIL)
	uv run --group calib python -m AtamuraOKK.calibration --xlsx "Чек лист встречи ОП - Январь.xlsx"

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
