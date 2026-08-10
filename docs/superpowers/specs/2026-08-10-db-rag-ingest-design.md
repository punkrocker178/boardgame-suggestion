# DB-backed RAG ingest

Date: 2026-08-10  
Status: approved design (pending implementation)

## Goal

Index Chroma from Postgres (crawled BGG catalog) instead of `data/games.csv`. Postgres is the sole source of truth for game documents at API startup. Remove the CSV export hop.

## Decisions

| Topic | Choice |
|-------|--------|
| Game source for ingest | Postgres query (same eligibility rules as former export) |
| CSV fallback | None — empty or unreachable DB fails startup |
| Vector store | Keep Chroma (unchanged retrieval path) |
| Change detection | Watermark `(eligible_count, max(updated_at))` under Chroma dir |
| Export script | Delete `scripts/export_games_csv.py` and its tests |
| Bundled CSV | Remove runtime use; delete `data/games.csv` and Dockerfile copy |

## Data flow

```text
import dump → crawl metadata → games (+ categories) in Postgres
                                    ↓
                         API lifespan: load eligible games
                                    ↓
                         documents → Chroma (skip if watermark unchanged)
                                    ↓
                         /recommend: retrieve + LLM (unchanged)
```

## Eligibility and mapping

Reuse the selection and field mapping previously in `scripts/export_games_csv.py`:

**Filter**

- `crawl_status == completed`
- `is_expansion == false`
- `min_players` IS NOT NULL
- `playing_time` IS NOT NULL

**Order**

- `rank` ascending nulls last, then `name` ascending

**Document fields**

| RAG field | Source |
|-----------|--------|
| `name` | `games.name` |
| `description` | `games.description` or fallback to `name` |
| `min_players` / `max_players` | `games.min_players` / `max_players` |
| `play_time_minutes` | `games.playing_time` |
| `categories` | join `game_categories`, lowercased with spaces → underscores; default `"strategy"` if none |
| `complexity` | `complexity_from_weight(weight)` when present |

**Embedded text** (unchanged): `{name}. {description}. Categories: {categories}.`

**Chroma metadata** (unchanged shape): `name`, `min_players`, `max_players`, `play_time_minutes`, `categories` (comma-joined), optional `complexity`.

## Components

### Shared loader

Put load/mapping in `app/ingest.py` (single place for RAG document pipeline):

- `load_games_for_rag(session) -> list[dict]` (string-valued rows matching the former CSV schema)
- `compute_db_watermark(session) -> str` (or derive watermark while loading) for skip/re-index
- No CSV I/O

### Ingest

- `ingest_games(..., force=False)` takes a DB session (or session factory), Chroma dir, embeddings — not a CSV path
- Watermark file under Chroma dir (replace `.games_csv_hash`), e.g. `.games_db_watermark` containing a stable string such as `{count}:{max_updated_at_iso_or_empty}`
- If watermark matches and not `force`: skip re-index; still report `indexed_count` as eligible row count
- If watermark differs or `force`: build documents, write Chroma collection, update watermark
- Zero eligible games → raise `IngestError` (do not start with an empty index as success)
- DB connection / query errors → propagate as ingest/startup failure

### Lifespan (`app/main.py`)

- Open session via existing `get_session_factory()` / `DATABASE_URL`
- Call DB-backed `ingest_games`
- Drop all `GAMES_CSV_PATH` usage

### Config / Compose / Docker

- Remove `games_csv_path` / `GAMES_CSV_PATH` from `app/config.py`, `.env.example`, `docker-compose.yml`
- Dockerfile: stop copying `data/games.csv`
- Local override may still bind-mount `./data` for Chroma persistence only

## Error handling

| Condition | Behavior |
|-----------|----------|
| Cannot connect to Postgres | Startup fails |
| Query error | Startup fails |
| Zero eligible games | `IngestError`, startup fails (message: import + crawl required) |
| Invalid row after mapping | Treat like today’s row validation failures (`IngestError`) |

No silent fallback to CSV or empty Chroma.

## Testing

- Unit-test loader mapping and eligibility with a real test DB session or factories seeding `Game` / `GameCategory` rows (prefer existing DB test patterns)
- Ingest skip/re-index when watermark unchanged vs when `updated_at` / count changes
- API tests: seed eligible games in DB; remove temp CSV + `GAMES_CSV_PATH` monkeypatch
- Delete `tests/test_export_games_csv.py`
- Keep FakeEmbeddings; no live embedding calls in CI

## Docs

- `docs/database.md`: remove “export to RAG CSV”; document that the API indexes from Postgres after import + crawl
- `README.md`: remove CSV replacement / `GAMES_CSV_PATH`; note DB prerequisite for ingest
- Historical specs under `docs/superpowers/` need not be rewritten; new behavior is this document

## Out of scope

- pgvector / removing Chroma
- Compose service that runs import/crawl automatically
- Startup readiness wait for “enough crawled games” beyond hard fail on zero
- Changing recommend/retrieve APIs or filter semantics
- Alembic or schema changes (use existing `games` / `game_categories` columns)

## Files to add/update/delete

| File | Action |
|------|--------|
| `app/ingest.py` | DB load, watermark, drop CSV path |
| `app/main.py` | Lifespan uses DB ingest |
| `app/config.py` | Remove `games_csv_path` |
| `scripts/export_games_csv.py` | Delete |
| `tests/test_export_games_csv.py` | Delete |
| `tests/test_ingest.py` | Rewrite for DB |
| `tests/test_api.py` | Seed DB instead of CSV |
| `data/games.csv` | Delete |
| `Dockerfile` | Drop CSV copy |
| `docker-compose.yml` | Drop `GAMES_CSV_PATH` |
| `.env.example` | Drop `GAMES_CSV_PATH` |
| `docs/database.md` | Update pipeline docs |
| `README.md` | Update dataset / env docs |

## Ops prerequisite

API start succeeds only when Postgres is reachable and at least one eligible game exists. Typical local sequence:

```bash
docker compose up -d db
python scripts/import_bgg_dump.py --csv boardgames_ranks.csv
python scripts/crawl_bgg_metadata.py
docker compose up --build   # or run API locally with DATABASE_URL
```

`depends_on: db` healthy does not imply crawl data is present.
