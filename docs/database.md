# Database operations

Postgres stores the BGG catalog (ranks dump, crawled metadata). Host scripts talk to Compose Postgres via `DATABASE_URL` on `localhost:5434`. The API container uses hostname `db` on the Compose network.

## Prerequisites

1. Copy and edit env: `cp .env.example .env`
2. Start Postgres (and optionally the API):

```bash
docker compose up -d db
# or full stack:
docker compose up --build
```

3. For Python scripts: venv + `pip install -r requirements.txt`, with host `DATABASE_URL` pointing at `localhost:5434` (see `.env.example`).

## Schema

On first start with an empty volume, Compose mounts `scripts/schema.sql` into `docker-entrypoint-initdb.d` and applies it once.

Import scripts may also call `init_db()` so tables exist when you run against an already-created database.

To re-run init SQL from scratch, remove the Postgres volume (destructive):

```bash
docker compose down -v
docker compose up -d db
```

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

The API indexes eligible completed non-expansion games from Postgres into Chroma on startup. There is no CSV export step. Startup fails if the database is unreachable or has no eligible games.

## Backup

Prefer custom format (`-Fc`): compressed and works with `pg_restore` options like `-a`.

Via Compose:

```bash
docker compose exec -T db pg_dump -U boardgame -d boardgame_suggestion -Fc -f /tmp/boardgame.dump
docker compose cp db:/tmp/boardgame.dump ./boardgame.dump
```

From the host (if `pg_dump` is installed):

```bash
PGPASSWORD=boardgame pg_dump -h localhost -p 5434 -U boardgame -d boardgame_suggestion -Fc -f boardgame.dump
```

Plain SQL alternative:

```bash
PGPASSWORD=boardgame pg_dump -h localhost -p 5434 -U boardgame -d boardgame_suggestion -f boardgame.sql
```

Use the same `POSTGRES_USER` / `POSTGRES_DB` values as in your `.env` if they differ from the examples above.

## Restore on another server

Copy `boardgame.dump` to the target machine. Ensure the database and role exist (e.g. start Compose there so `POSTGRES_*` create them).

### Empty database (no schema yet)

Full restore (schema + data):

```bash
PGPASSWORD=boardgame pg_restore -h <host> -p <port> -U boardgame -d boardgame_suggestion --clean --if-exists boardgame.dump
```

With Docker on the target:

```bash
docker compose cp ./boardgame.dump db:/tmp/boardgame.dump
docker compose exec -T db pg_restore -U boardgame -d boardgame_suggestion --clean --if-exists /tmp/boardgame.dump
```

`--clean --if-exists` drops existing objects first (destructive on the target).

### Schema already initialized

If the target already ran `scripts/schema.sql` or `init_db()`, restore **data only** (`-a`). Do not use `--clean`.

```bash
PGPASSWORD=boardgame pg_restore -h <host> -p <port> -U boardgame -d boardgame_suggestion -a --disable-triggers boardgame.dump
```

With Docker:

```bash
docker compose cp ./boardgame.dump db:/tmp/boardgame.dump
docker compose exec -T db pg_restore -U boardgame -d boardgame_suggestion -a --disable-triggers /tmp/boardgame.dump
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
