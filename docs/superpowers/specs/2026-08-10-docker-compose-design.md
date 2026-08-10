# Docker Compose (Postgres + FastAPI)

Date: 2026-08-10  
Status: approved design (pending implementation)

## Goal

Run the Board Game Suggestion stack with Docker Compose: Postgres and FastAPI. Support local development and a production-like path via base compose + override.

## Decisions

| Topic | Choice |
|-------|--------|
| Layout | Base `docker-compose.yml` + auto-merged `docker-compose.override.yml` for local |
| Schema bootstrap | Mount `scripts/schema.sql` into Postgres `docker-entrypoint-initdb.d` (runs once on empty volume) |
| Config | Compose `env_file: .env`; document vars in `.env.example` |
| Postgres host port | `5434` → container `5432` |
| API host port | `8000` |

## Services

### `db`

- Image: `postgres:16`
- Env from `.env`: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`
- Volume: named volume for data persistence
- Init: bind-mount `./scripts/schema.sql` → `/docker-entrypoint-initdb.d/01-schema.sql`
- Ports: `5434:5432`
- Healthcheck: `pg_isready`

### `api`

- Build: `Dockerfile` (Python slim, install `requirements.txt`, `COPY` app + `data/games.csv`, uvicorn `app.main:app` on `0.0.0.0:8000`)
- `env_file: .env`
- Override `DATABASE_URL` to use hostname `db` (Compose network), e.g. `postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}` (this overrides any host `localhost:5434` value in `.env`)
- `depends_on` with condition: `db` healthy
- Ports: `8000:8000`

## Dev vs prod

### Local (`docker compose up`)

Override merges automatically:

- Bind-mount project source into the API container
- Uvicorn with `--reload`
- Bind-mount `./data` for CSV / Chroma persistence on the host

### Production-like

```bash
docker compose -f docker-compose.yml up --build
```

Uses the built image only (no reload, no source mount). Same published ports and Postgres init behavior.

## Config / networking

- Host scripts (e.g. `scripts/import_bgg_dump.py`) use `DATABASE_URL` with `localhost:5434`
- API container uses `db:5432` via Compose environment override
- LLM / embedding keys and path settings remain in `.env` as today
- `.env.example` updated with Postgres vars and the host-oriented `DATABASE_URL` example

## Out of scope

- Alembic migration runner in Compose
- Shipping seed/import as a Compose service
- TLS, reverse proxy, or cloud deploy wiring
- Changing FastAPI lifespan to call `init_db()` (schema comes from init SQL on first volume)

## Files to add/update

| File | Action |
|------|--------|
| `Dockerfile` | Add |
| `docker-compose.yml` | Add |
| `docker-compose.override.yml` | Add |
| `.dockerignore` | Add (exclude `.venv`, `.git`, `data/chroma`, caches) |
| `.env.example` | Update Postgres + host `DATABASE_URL` (`localhost:5434`) |
| `README.md` | Add brief Compose usage section |

## Usage sketch

```bash
cp .env.example .env   # set secrets
docker compose up --build
# Host import against Compose DB:
# DATABASE_URL=postgresql://USER:PASS@localhost:5434/DB python scripts/import_bgg_dump.py ...
```
