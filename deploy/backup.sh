#!/usr/bin/env bash
# Nightly backup of the two stateful stores that ARE the product:
#   - Postgres  (calls, transcripts, scores, companion_users, meetings)  -> pg_dump
#   - MinIO     (call recordings)                                        -> volume tar
#
# Run from the AtamuraOKK repo root (where docker-compose.yml lives), or set
# COMPOSE_DIR. Schedule via deploy/okk-backup.timer or cron (see footer).
# Restore instructions are at the bottom of this file.
set -euo pipefail

COMPOSE_DIR="${COMPOSE_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/atamuraokk}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
DB_SERVICE="${DB_SERVICE:-db}"
DB_USER="${ATAMURAOKK_DB_USER:-AtamuraOKK}"
DB_BASE="${ATAMURAOKK_DB_BASE:-AtamuraOKK}"
MINIO_VOLUME="${MINIO_VOLUME:-AtamuraOKK-minio-data}"
STAMP="$(date +%Y%m%d-%H%M%S)"

cd "$COMPOSE_DIR"
mkdir -p "$BACKUP_DIR"

echo "[backup] $STAMP -> $BACKUP_DIR"

# --- Postgres: logical dump, compressed, atomic via tmp+rename --------------
db_tmp="$BACKUP_DIR/.pg-${DB_BASE}-${STAMP}.sql.gz.partial"
db_out="$BACKUP_DIR/pg-${DB_BASE}-${STAMP}.sql.gz"
echo "[backup] pg_dump $DB_BASE ..."
docker compose exec -T "$DB_SERVICE" pg_dump -U "$DB_USER" "$DB_BASE" | gzip -c > "$db_tmp"
mv "$db_tmp" "$db_out"
echo "[backup] wrote $db_out ($(du -h "$db_out" | cut -f1))"

# --- MinIO: tar the named volume (recordings are immutable; hot copy is fine) -
minio_out="$BACKUP_DIR/minio-${STAMP}.tar.gz"
echo "[backup] minio volume $MINIO_VOLUME ..."
docker run --rm -v "$MINIO_VOLUME":/data:ro -v "$BACKUP_DIR":/backup alpine \
  sh -c "tar czf /backup/minio-${STAMP}.tar.gz -C /data ."
echo "[backup] wrote $minio_out ($(du -h "$minio_out" | cut -f1))"

# --- Retention --------------------------------------------------------------
find "$BACKUP_DIR" -maxdepth 1 -type f \( -name 'pg-*.sql.gz' -o -name 'minio-*.tar.gz' \) \
  -mtime +"$RETENTION_DAYS" -print -delete

echo "[backup] done. Kept last ${RETENTION_DAYS} days."

# ─────────────────────────── RESTORE (manual) ───────────────────────────────
# Postgres (DESTRUCTIVE — drops+recreates the DB):
#   gunzip -c pg-AtamuraOKK-YYYYMMDD-HHMMSS.sql.gz | \
#     docker compose exec -T db psql -U AtamuraOKK -d postgres \
#       -c "DROP DATABASE IF EXISTS AtamuraOKK; CREATE DATABASE AtamuraOKK OWNER AtamuraOKK;"
#   gunzip -c pg-AtamuraOKK-YYYYMMDD-HHMMSS.sql.gz | \
#     docker compose exec -T db psql -U AtamuraOKK -d AtamuraOKK
#
# MinIO (stop writers first, then replace the volume contents):
#   docker compose stop download transcribe score dispatcher meetings-worker
#   docker run --rm -v AtamuraOKK-minio-data:/data -v /var/backups/atamuraokk:/backup alpine \
#     sh -c "rm -rf /data/* && tar xzf /backup/minio-YYYYMMDD-HHMMSS.tar.gz -C /data"
#   docker compose start dispatcher download transcribe score meetings-worker
#
# ─────────────────────────── SCHEDULE ───────────────────────────────────────
# systemd: install deploy/okk-backup.service + deploy/okk-backup.timer, then
#   systemctl enable --now okk-backup.timer
# cron (alternative):
#   30 3 * * *  cd /opt/AtamuraOKK && ./deploy/backup.sh >> /var/log/okk-backup.log 2>&1
