# Docker Compose (Postgres + FastAPI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Docker Compose so Postgres and the FastAPI app run together for local dev (with override) and production-like runs (base file only).

**Architecture:** `postgres:16` service mounts `scripts/schema.sql` for first-boot DDL and publishes host port `5434`. FastAPI builds from a `Dockerfile` and uses Compose-overridden `DATABASE_URL` pointing at hostname `db`. `docker-compose.override.yml` adds bind mounts and uvicorn `--reload` for local work.

**Tech Stack:** Docker Compose v2, Postgres 16, Python slim image, uvicorn, existing FastAPI app (`app.main:app`)

**Spec:** `docs/superpowers/specs/2026-08-10-docker-compose-design.md`

## Global Constraints

- Postgres host port mapping: `5434:5432` (container listens on 5432)
- API host port: `8000:8000`
- Schema init: mount `./scripts/schema.sql` → `/docker-entrypoint-initdb.d/01-schema.sql` (runs only on empty data volume)
- Config via `env_file: .env`; document in `.env.example`
- API container `DATABASE_URL` must use hostname `db`, not `localhost`
- Local: `docker compose up` (auto-merges override). Prod-like: `docker compose -f docker-compose.yml up --build`
- Do not add Alembic runner, import service, TLS, or FastAPI `init_db()` on lifespan
- Do not commit `.env` or secrets; only update `.env.example`

---

## File map

| File | Responsibility |
|------|----------------|
| `Dockerfile` | Build API image: deps, copy app + `data/games.csv`, run uvicorn |
| `.dockerignore` | Keep image small; exclude venv, git, chroma, caches |
| `docker-compose.yml` | Base: `db` + `api`, healthcheck, ports, volumes, env |
| `docker-compose.override.yml` | Local: source bind-mount, `--reload`, `./data` mount |
| `.env.example` | Document `POSTGRES_*` and host `DATABASE_URL` with port 5434 |
| `README.md` | Compose quick-start section |

---

### Task 1: Dockerfile and `.dockerignore`

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`

**Interfaces:**
- Consumes: `requirements.txt`, `app/`, `data/games.csv`
- Produces: image that runs `uvicorn app.main:app --host 0.0.0.0 --port 8000`

- [ ] **Step 1: Create `.dockerignore`**

```
.venv/
.git/
.pytest_cache/
__pycache__/
*.py[cod]
.env
data/chroma/
docs/
*.md
boardgames_ranks.csv
.vscode/
pytest-cache-files-*/
.coverage
htmlcov/
```

- [ ] **Step 2: Create `Dockerfile`**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data/games.csv ./data/games.csv

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 3: Verify image builds**

Run: `docker build -t boardgame-suggestion-api .`

Expected: build succeeds (may take a few minutes for pip).

- [ ] **Step 4: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "$(cat <<'EOF'
Add Dockerfile and dockerignore for the FastAPI service.

EOF
)"
```

---

### Task 2: Base `docker-compose.yml`

**Files:**
- Create: `docker-compose.yml`

**Interfaces:**
- Consumes: `Dockerfile`, `.env` vars `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`
- Produces: services `db` and `api`; named volume `pgdata`; network default

- [ ] **Step 1: Create `docker-compose.yml`**

```yaml
services:
  db:
    image: postgres:16
    env_file: .env
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    ports:
      - "5434:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./scripts/schema.sql:/docker-entrypoint-initdb.d/01-schema.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 5s
      timeout: 5s
      retries: 10

  api:
    build: .
    env_file: .env
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}
      GAMES_CSV_PATH: ./data/games.csv
      CHROMA_PERSIST_DIR: ./data/chroma
    ports:
      - "8000:8000"
    depends_on:
      db:
        condition: service_healthy

volumes:
  pgdata:
```

- [ ] **Step 2: Ensure a temporary `.env` exists for config validation** (do not commit)

If `.env` is missing, copy from example and set at least:

```
POSTGRES_USER=boardgame
POSTGRES_PASSWORD=boardgame
POSTGRES_DB=boardgame_suggestion
DATABASE_URL=postgresql://boardgame:boardgame@localhost:5434/boardgame_suggestion
```

(Keep any existing LLM keys already in `.env`.)

- [ ] **Step 3: Validate Compose file**

Run: `docker compose -f docker-compose.yml config`

Expected: prints merged config without errors; `api.environment.DATABASE_URL` uses `@db:5432`; `db.ports` shows `5434:5432`.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml
git commit -m "$(cat <<'EOF'
Add base Compose stack for Postgres and FastAPI.

EOF
)"
```

---

### Task 3: Local override

**Files:**
- Create: `docker-compose.override.yml`

**Interfaces:**
- Consumes: base `api` service from Task 2
- Produces: reload + bind mounts when running plain `docker compose up`

- [ ] **Step 1: Create `docker-compose.override.yml`**

```yaml
services:
  api:
    volumes:
      - ./:/app
      - ./data:/app/data
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
```

- [ ] **Step 2: Validate merged local config**

Run: `docker compose config`

Expected: `api` command includes `--reload`; volumes include project bind-mount and `./data`.

- [ ] **Step 3: Validate prod-like config ignores override**

Run: `docker compose -f docker-compose.yml config`

Expected: `api` command is image default (no `--reload` in the command override).

- [ ] **Step 4: Commit**

```bash
git add docker-compose.override.yml
git commit -m "$(cat <<'EOF'
Add Compose override for local reload and data mounts.

EOF
)"
```

---

### Task 4: `.env.example` and README

**Files:**
- Modify: `.env.example`
- Modify: `README.md`

**Interfaces:**
- Consumes: Compose var names from Task 2
- Produces: documented host `DATABASE_URL` on port 5434 and Compose usage

- [ ] **Step 1: Update `.env.example`** to:

```
LLM_PROVIDER=openrouter
EMBEDDING_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=openai/gpt-4o-mini
OPENROUTER_EMBEDDING_MODEL=openai/text-embedding-3-small
GAMES_CSV_PATH=./data/games.csv
CHROMA_PERSIST_DIR=./data/chroma
LOG_LEVEL=INFO
POSTGRES_USER=boardgame
POSTGRES_PASSWORD=boardgame
POSTGRES_DB=boardgame_suggestion
# Host tools (import scripts) against Compose Postgres:
DATABASE_URL=postgresql://boardgame:boardgame@localhost:5434/boardgame_suggestion
BGG_API_TOKEN=
BGG_REQUEST_DELAY_SECONDS=5
BGG_BATCH_SIZE=20
```

- [ ] **Step 2: Add a Compose section to `README.md`** after Quick Start:

```markdown
## Docker Compose

```bash
cp .env.example .env
# Edit .env: set OPENROUTER_API_KEY and Postgres credentials
docker compose up --build
```

- API: http://localhost:8000
- Postgres (host): `localhost:5434` (use `DATABASE_URL` from `.env` for scripts)

Production-like (no reload / no source mount):

```bash
docker compose -f docker-compose.yml up --build
```

Schema is applied automatically on first Postgres volume init via `scripts/schema.sql`.
```

Place this section after the existing Quick Start block (before `## API`).

- [ ] **Step 3: Commit**

```bash
git add .env.example README.md
git commit -m "$(cat <<'EOF'
Document Compose env vars and usage in README.

EOF
)"
```

---

### Task 5: End-to-end verification

**Files:**
- None (verification only)

**Interfaces:**
- Consumes: all artifacts from Tasks 1–4; requires Docker daemon and a `.env` with `POSTGRES_*` set

- [ ] **Step 1: Start the stack**

Run: `docker compose up --build -d`

Expected: both containers start; `db` becomes healthy.

- [ ] **Step 2: Confirm schema exists**

Run:

```bash
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c '\dt'
```

(Or substitute user/db from `.env`, e.g. `-U boardgame -d boardgame_suggestion`.)

Expected: tables `games`, `game_categories`, `game_mechanics` listed.

- [ ] **Step 3: Hit API health**

Run: `curl -s http://localhost:8000/health`

Expected: JSON with `"status"` of `"ok"` or `"degraded"` (degraded is acceptable if indexing/LLM fails without a key; process must still respond).

- [ ] **Step 4: Confirm host port 5434**

Run: `pg_isready -h localhost -p 5434` (if client installed) **or**

```bash
docker compose exec db pg_isready -U boardgame -d boardgame_suggestion
```

and verify host mapping with:

```bash
docker compose port db 5432
```

Expected: `0.0.0.0:5434` (or similar).

- [ ] **Step 5: Tear down (optional)**

Run: `docker compose down`

Do **not** use `down -v` unless intentionally wiping the Postgres volume.

- [ ] **Step 6: No commit** unless verification required fixing files; if fixes were needed, commit those fixes with a clear message.

---

## Spec coverage checklist

| Spec requirement | Task |
|------------------|------|
| Base + override layout | 2, 3 |
| `schema.sql` init mount | 2 |
| `.env` / `.env.example` | 2, 4 |
| Host port 5434 | 2, 5 |
| API port 8000 | 2, 5 |
| `DATABASE_URL` → `db` for API | 2 |
| Dockerfile copies app + games.csv | 1 |
| `.dockerignore` | 1 |
| Local reload + data mount | 3 |
| Prod-like `-f docker-compose.yml` | 3, 4 |
| README usage | 4 |
| Out of scope (Alembic, import service, TLS, lifespan init_db) | — intentionally omitted |
