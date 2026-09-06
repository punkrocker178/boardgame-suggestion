-- One-shot: topic epoch for multi-turn /recommend.
-- Fresh installs: updated scripts/schema.sql already has the column (skip this file).

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS topic_started_at TIMESTAMPTZ;
