-- One-shot for existing Postgres DBs. Run manually against the app DB.
-- Fresh installs: use updated scripts/schema.sql (skip this file).
-- SQLite tests do not run this file.
BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_games_name_trgm ON games USING gin (name gin_trgm_ops);

COMMIT;
