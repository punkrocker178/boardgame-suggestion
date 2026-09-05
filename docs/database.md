# Database operations

Postgres stores the BGG catalog (ranks dump, crawled metadata). Host scripts talk to Compose Postgres via `DATABASE_URL` on `localhost:5434`. The API container uses hostname `db` on the Compose network.

Compose CLI: use **Docker** (`docker compose`) or **Podman** (`podman-compose`). Examples below show both where they differ.

## Prerequisites

1. Copy and edit env: `cp .env.example .env`
2. Start Postgres (and optionally the API):

```bash
# Docker
docker compose up -d db
docker compose up --build

# Podman
podman-compose up -d db
podman-compose up --build
```

3. For Python scripts: venv + `pip install -r requirements.txt`, with host `DATABASE_URL` pointing at `localhost:5434` (see `.env.example`).

## Schema

On first start with an empty volume, Compose mounts `scripts/schema.sql` into `docker-entrypoint-initdb.d` and applies it once.

Import scripts may also call `init_db()` so tables exist when you run against an already-created database.

To re-run init SQL from scratch, remove the Postgres volume (destructive):

```bash
# Docker
docker compose down -v
docker compose up -d db

# Podman
podman-compose down -v
podman-compose up -d db
```

### Taxonomy FK + poll-summary migration (existing DBs)

If the database was created before categories/mechanics lookup tables and poll arrays:

1. Apply `scripts/migrate_taxonomy_poll_summary.sql` (drops old string junctions, adds FK junctions + array columns, resets `crawl_status` to `pending`).
2. Run `python scripts/crawl_bgg_metadata.py` — restart alone is not enough.
3. Fresh installs: use updated `scripts/schema.sql` + import dump + crawl; skip the migrate script.

### pg_trgm name index (existing DBs)

Needed for similar-to name lookup (and later catalog search):

1. Apply `scripts/migrate_pg_trgm.sql`.
2. Fresh installs: updated `scripts/schema.sql` already creates the extension and GIN index; skip this migrate script.

### Conversations (existing DBs)

Needed for multi-turn `/recommend`:

1. Apply `scripts/migrate_conversations.sql`.
2. Fresh installs: updated `scripts/schema.sql` already creates the tables; skip this migrate script.

## Import BGG dump

Download a BGG ranks CSV (e.g. `boardgames_ranks.csv`), then:

```bash
python scripts/import_bgg_dump.py --csv boardgames_ranks.csv
```

- Upserts by game id (idempotent).
- Commits in batches (`--commit-batch-size`, default `1000`).
- Safe to re-run after a crash; already-committed rows are mostly skipped.

## Crawl metadata for RAG

After the dump is loaded:

```bash
# Fetch full metadata from the BGG API (needs BGG_API_TOKEN in .env)
python scripts/crawl_bgg_metadata.py
```

The API indexes eligible completed non-expansion games from Postgres into Chroma on startup. There is no CSV export step. Startup fails if the database is unreachable or has no eligible games **and** no prior Chroma index exists.

Re-indexing builds into a staging directory and swaps into the live Chroma dir only after a full success (watermark updated last). If embedding fails mid-refresh, the previous live index is kept and `/health` may report `degraded` (stale).

Chroma metadata includes players, play time, categories, complexity buckets, raw `weight`, `min_age`, `year_published`, and poll lists (`best_with_players` / `recommended_with_players` as `#n#` tokens). Poll filters apply only when the user explicitly asks (e.g. “best with 4”); plain “for 4 players” uses the box player range.

## Chroma backup

Live index only (`CHROMA_PERSIST_DIR`, default `./data/chroma`). Staging and `chroma_old` are not included.

Stop the API first so `chroma.sqlite3` is complete (wait until ingest/swap finished). Scripts do not lock Chroma.

Canonical local path: `data/chroma.tar.gz` (overwrites that file, via a temp file).

```bash
# Stop uvicorn / compose api first
python scripts/backup_chroma.py
```

Requires live `.games_db_watermark`. If that file is missing, backup fails and the previous tarball is left in place. After a successful ingest swap, the watermark is present.

Restore (API stopped) replaces the live persist dir. `chroma_staging` is left alone.

```bash
python scripts/restore_chroma.py
```

Copy `data/chroma.tar.gz` to another machine, place it next to that host’s persist dir (`<persist_dir>.tar.gz`), restore, then start the API. Startup skips re-index only when the restored watermark equals the current eligible-game DB watermark (`count:max(updated_at)`). If the catalog has changed, ingest refreshes as usual (staging resume unchanged).

## Backup

Prefer custom format (`-Fc`): compressed and works with `pg_restore` options like `-a`.

Canonical local path: `data/boardgame.dump` (overwrites that file).

Via Compose:

```bash
# Docker
docker compose exec -T db pg_dump -U boardgame -d boardgame_suggestion -Fc -f /tmp/boardgame.dump
docker compose cp db:/tmp/boardgame.dump ./data/boardgame.dump

# Podman (stdout redirect; podman-compose has no `cp` subcommand)
podman-compose exec -T db pg_dump -U boardgame -d boardgame_suggestion -Fc > data/boardgame.dump
```

Equivalent with container name + engine `cp`:

```bash
# Docker
docker exec boardgame-suggestion-db-1 pg_dump -U boardgame -d boardgame_suggestion -Fc -f /tmp/boardgame.dump
docker cp boardgame-suggestion-db-1:/tmp/boardgame.dump ./data/boardgame.dump

# Podman
podman exec boardgame-suggestion_db_1 pg_dump -U boardgame -d boardgame_suggestion -Fc -f /tmp/boardgame.dump
podman cp boardgame-suggestion_db_1:/tmp/boardgame.dump ./data/boardgame.dump
```

From the host (if `pg_dump` is installed):

```bash
PGPASSWORD=boardgame pg_dump -h localhost -p 5434 -U boardgame -d boardgame_suggestion -Fc -f data/boardgame.dump
```

Plain SQL alternative:

```bash
PGPASSWORD=boardgame pg_dump -h localhost -p 5434 -U boardgame -d boardgame_suggestion -f boardgame.sql
```

Use the same `POSTGRES_USER` / `POSTGRES_DB` values as in your `.env` if they differ from the examples above.

## Restore on another server

Copy `data/boardgame.dump` to the target machine. Ensure the database and role exist (e.g. start Compose there so `POSTGRES_*` create them).

### Empty database (no schema yet)

Full restore (schema + data):

```bash
PGPASSWORD=boardgame pg_restore -h <host> -p <port> -U boardgame -d boardgame_suggestion --clean --if-exists data/boardgame.dump
```

With Compose on the target:

```bash
# Docker
docker compose cp ./data/boardgame.dump db:/tmp/boardgame.dump
docker compose exec -T db pg_restore -U boardgame -d boardgame_suggestion --clean --if-exists /tmp/boardgame.dump

# Podman
podman-compose exec -T db pg_restore -U boardgame -d boardgame_suggestion --clean --if-exists - < data/boardgame.dump
```

Or with engine `cp` / `exec`:

```bash
# Docker
docker cp ./data/boardgame.dump boardgame-suggestion-db-1:/tmp/boardgame.dump
docker exec boardgame-suggestion-db-1 pg_restore -U boardgame -d boardgame_suggestion --clean --if-exists /tmp/boardgame.dump

# Podman
podman cp ./data/boardgame.dump boardgame-suggestion_db_1:/tmp/boardgame.dump
podman exec boardgame-suggestion_db_1 pg_restore -U boardgame -d boardgame_suggestion --clean --if-exists /tmp/boardgame.dump
```

`--clean --if-exists` drops existing objects first (destructive on the target).

### Schema already initialized

If the target already ran `scripts/schema.sql` or `init_db()`, restore **data only** (`-a`). Do not use `--clean`.

```bash
PGPASSWORD=boardgame pg_restore -h <host> -p <port> -U boardgame -d boardgame_suggestion -a --disable-triggers data/boardgame.dump
```

With Compose:

```bash
# Docker
docker compose cp ./data/boardgame.dump db:/tmp/boardgame.dump
docker compose exec -T db pg_restore -U boardgame -d boardgame_suggestion -a --disable-triggers /tmp/boardgame.dump

# Podman
podman-compose exec -T db pg_restore -U boardgame -d boardgame_suggestion -a --disable-triggers - < data/boardgame.dump
```

Or with engine `cp` / `exec`:

```bash
# Docker
docker cp ./data/boardgame.dump boardgame-suggestion-db-1:/tmp/boardgame.dump
docker exec boardgame-suggestion-db-1 pg_restore -U boardgame -d boardgame_suggestion -a --disable-triggers /tmp/boardgame.dump

# Podman
podman cp ./data/boardgame.dump boardgame-suggestion_db_1:/tmp/boardgame.dump
podman exec boardgame-suggestion_db_1 pg_restore -U boardgame -d boardgame_suggestion -a --disable-triggers /tmp/boardgame.dump
```

Plain SQL restore:

```bash
PGPASSWORD=boardgame psql -h <host> -p <port> -U boardgame -d boardgame_suggestion -f boardgame.sql
```

(For plain SQL into a DB that already has schema, dump with `--data-only` / `-a` on the source, or edit carefully — prefer custom format + `pg_restore -a`.)

## Notes

- A normal dump includes schema and data; after a successful restore you do not need to re-run the BGG dump import.
- Host port for local Compose is `5434` → container `5432`.
- Keep dump files out of git if they are large; add them to `.gitignore` as needed.
- Container names differ by engine/project naming (`boardgame-suggestion-db-1` vs `boardgame-suggestion_db_1`); check with `docker ps` / `podman ps`.
- `podman compose` (built-in) may delegate to `docker-compose` and fail if the Podman socket is not set; prefer `podman-compose` or raw `podman exec` / `podman cp`.
- Do not pass `-T` to plain `podman exec` / `docker exec` (Compose-only flag); use `*-compose exec -T` or engine `exec` without `-T`.
