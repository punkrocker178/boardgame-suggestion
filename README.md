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
| `GAMES_CSV_PATH` | `./data/games.csv` | Path to games dataset |
| `CHROMA_PERSIST_DIR` | `./data/chroma` | Chroma persistence directory |

See `.env.example` for all options.

## Data

Replace `data/games.csv` or point `GAMES_CSV_PATH` at your own file. The app re-indexes automatically when the file hash changes.

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
