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

# --- Phase 0 transcription spike ---
spike-fetch: ## Pull recent answered+recorded calls (telephony scope)
	uv run python -m AtamuraOKK.spike fetch

spike-download: ## Download recordings (needs disk scope for external-integration calls)
	uv run python -m AtamuraOKK.spike download

spike-transcribe: ## Transcribe with faster-whisper (needs spike group + ffmpeg)
	uv run python -m AtamuraOKK.spike transcribe

spike-wer: ## Compute WER vs hand-corrected references
	uv run python -m AtamuraOKK.spike wer
