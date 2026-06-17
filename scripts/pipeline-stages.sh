#!/usr/bin/env bash
#
# Pause / resume call transcription and scoring.
#
# These two stages run as their own docker compose worker services
# (`transcribe`, `score`). Stopping them cleanly pauses transcription and
# scoring while everything else keeps running: ingestion still pulls new
# calls, the download stage still fetches recordings (backlog builds up at
# DOWNLOADED / TRANSCRIBED), the API stays up, and reports keep generating.
#
# Pausing is safe and lossless: the pipeline's source of truth is Postgres
# (each call's `status`), and Redis is a transient queue the dispatcher
# rebuilds every tick. When you resume, the workers pick the backlog up where
# it left off — nothing is dropped.
#
# This controls the CALL pipeline only. The ОП meeting pipeline
# (`meetings-worker`) is separate and is not touched.
#
# Usage:
#   ./scripts/pipeline-stages.sh stop      # pause transcription + scoring
#   ./scripts/pipeline-stages.sh start     # resume transcription + scoring
#   ./scripts/pipeline-stages.sh status    # show current state
#   ./scripts/pipeline-stages.sh restart   # stop then start
#
# Aliases: pause=stop, continue/resume=start.
#
# Limit to one stage with STAGES, e.g.:
#   STAGES="score" ./scripts/pipeline-stages.sh stop     # pause scoring only
#   STAGES="transcribe" ./scripts/pipeline-stages.sh stop

set -euo pipefail

# Default to both transcription and scoring; override with STAGES.
STAGES="${STAGES:-transcribe score}"

# Run compose from the project root (this script lives in scripts/).
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

action="${1:-status}"

case "$action" in
  stop | pause)
    echo "Pausing: $STAGES"
    docker compose stop $STAGES
    ;;
  start | continue | resume)
    echo "Resuming: $STAGES"
    docker compose start $STAGES
    ;;
  restart)
    echo "Restarting: $STAGES"
    docker compose restart $STAGES
    ;;
  status)
    docker compose ps $STAGES
    ;;
  *)
    echo "Unknown action: $action" >&2
    echo "Usage: $0 {stop|start|restart|status}  (STAGES=\"transcribe score\")" >&2
    exit 2
    ;;
esac
