-- Dedicated app-db for Metabase's internal metadata (users, dashboards, settings).
-- Kept separate from the AtamuraOKK application database. Runs only on first
-- Postgres volume initialization (docker-entrypoint-initdb.d). For an already
-- initialized volume, create it manually:
--   docker compose exec db psql -U AtamuraOKK -c "CREATE DATABASE metabaseappdb;"
CREATE DATABASE metabaseappdb;
