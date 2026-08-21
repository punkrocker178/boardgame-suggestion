-- Bootstrap DDL for BGG pipeline (also created via SQLAlchemy init_db)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY,
    name VARCHAR(512) NOT NULL,
    year_published INTEGER,
    rank INTEGER,
    bayes_average NUMERIC(10, 5),
    average NUMERIC(10, 5),
    users_rated INTEGER,
    is_expansion BOOLEAN NOT NULL DEFAULT FALSE,
    abstracts_rank INTEGER,
    cgs_rank INTEGER,
    childrensgames_rank INTEGER,
    familygames_rank INTEGER,
    partygames_rank INTEGER,
    strategygames_rank INTEGER,
    thematic_rank INTEGER,
    wargames_rank INTEGER,
    description TEXT,
    min_players INTEGER,
    max_players INTEGER,
    playing_time INTEGER,
    min_play_time INTEGER,
    max_play_time INTEGER,
    min_age INTEGER,
    weight NUMERIC(4, 2),
    thumbnail_url TEXT,
    image_url TEXT,
    best_with_players INTEGER[],
    recommended_with_players INTEGER[],
    crawl_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    crawled_at TIMESTAMPTZ,
    crawl_attempts INTEGER NOT NULL DEFAULT 0,
    last_crawl_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS mechanics (
    id INTEGER PRIMARY KEY,
    name VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS game_categories (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    PRIMARY KEY (game_id, category_id)
);

CREATE TABLE IF NOT EXISTS game_mechanics (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    mechanic_id INTEGER NOT NULL REFERENCES mechanics(id),
    PRIMARY KEY (game_id, mechanic_id)
);

CREATE INDEX IF NOT EXISTS idx_games_crawl_status ON games(crawl_status);
CREATE INDEX IF NOT EXISTS idx_games_rank ON games(rank) WHERE rank IS NOT NULL AND rank > 0;
CREATE INDEX IF NOT EXISTS idx_games_name_trgm ON games USING gin (name gin_trgm_ops);
