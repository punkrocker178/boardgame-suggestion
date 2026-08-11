-- One-shot for existing Postgres DBs (option 1). Run manually against the app DB.
-- After this: run scripts/crawl_bgg_metadata.py to refill taxonomy + poll arrays.
-- Fresh installs: use updated scripts/schema.sql + import dump + crawl (skip this file).
BEGIN;

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS mechanics (
    id INTEGER PRIMARY KEY,
    name VARCHAR(255) NOT NULL
);

DROP TABLE IF EXISTS game_categories;
DROP TABLE IF EXISTS game_mechanics;

CREATE TABLE game_categories (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    PRIMARY KEY (game_id, category_id)
);

CREATE TABLE game_mechanics (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    mechanic_id INTEGER NOT NULL REFERENCES mechanics(id),
    PRIMARY KEY (game_id, mechanic_id)
);

ALTER TABLE games
    ADD COLUMN IF NOT EXISTS best_with_players INTEGER[],
    ADD COLUMN IF NOT EXISTS recommended_with_players INTEGER[];

UPDATE games
SET crawl_status = 'pending',
    crawled_at = NULL,
    crawl_attempts = 0,
    last_crawl_error = NULL;

COMMIT;
