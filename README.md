# Board Game RAG Game Master

A learning-focused LangChain RAG application that recommends board games from natural-language queries.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set OPENROUTER_API_KEY
uvicorn app.main:app --reload
```

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

Database ops (BGG dump import, backup, restore): [docs/database.md](docs/database.md).

## API

### `GET /health`

Returns indexing status and game count.

### `POST /recommend`

```json
{ "query": "Suggest a light strategy game for 4 players under 60 minutes" }
```

Returns ranked recommendations with reasoning and applied filters.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `openrouter` | `openrouter`, `openai`, or `ollama` |
| `EMBEDDING_PROVIDER` | `openrouter` | Same options as LLM |
| `OPENROUTER_API_KEY` | — | API key for OpenRouter |
| `DATABASE_URL` | see `.env.example` | Postgres URL (host scripts use `localhost:5434`) |
| `CHROMA_PERSIST_DIR` | `./data/chroma` | Chroma persistence directory |

See `.env.example` for all options.

## Data

1. Start Postgres (`docker compose up -d db`).
2. Import a BGG ranks dump (`python scripts/import_bgg_dump.py --csv ...`).
3. Crawl metadata (`python scripts/crawl_bgg_metadata.py`).
4. Start the API — it re-indexes Chroma when the DB watermark (eligible count + max `updated_at`) changes.

See [docs/database.md](docs/database.md).

## Testing

```bash
pytest
```

Tests mock LLM calls — no live API key required.

## Architecture

```
POST /recommend → query extraction (LLM) → filtered Chroma retrieval → synthesis (LLM) → JSON
```

See [design spec](docs/superpowers/specs/2026-06-22-boardgame-rag-design.md) for full details.
