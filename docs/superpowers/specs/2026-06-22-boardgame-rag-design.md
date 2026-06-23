# Board Game RAG Game Master — Design Spec

**Date:** 2026-06-22  
**Status:** Approved  
**Purpose:** Learning-focused LangChain RAG application that recommends board games based on natural-language user requests.

---

## Overview

A FastAPI API that acts as a board game "game master." Users send natural-language queries (e.g., "Suggest a light strategy game for 4 players under 60 minutes") and receive ranked game recommendations with reasoning.

The app uses a query-understanding chain: LLM extracts structured filters → Chroma retrieves with metadata filters + semantic search → LLM synthesizes recommendations.

---

## Requirements Summary


| Decision       | Choice                                                                                   |
| -------------- | ---------------------------------------------------------------------------------------- |
| Interface      | FastAPI API only                                                                         |
| Data source    | Bundled starter CSV (~25 games); swap via env var later                                  |
| Data format    | CSV (flat columns)                                                                       |
| LLM/Embeddings | Configurable via env vars; **OpenRouter for both** is the primary setup (single API key) |
| Vector store   | Chroma with metadata filters                                                             |
| Query style    | Natural language only                                                                    |
| Conversation   | One-shot v1; API designed for future multi-turn                                          |


---

## Architecture

```
Client → POST /recommend { "query": "..." }
              ↓
         FastAPI handler
              ↓
    ┌─────────┴─────────┐
    ↓                   ↓
 Query extractor     (startup) CSV → ingest → Chroma
 (LLM structured)              ↑
    ↓                    re-index on file change
 Filtered retriever
 (Chroma metadata + similarity)
    ↓
 Recommendation synthesizer (LLM)
    ↓
 JSON response { recommendations[], reasoning, filters_applied }
```

### Components


| Unit                 | Responsibility                                                        |
| -------------------- | --------------------------------------------------------------------- |
| `config.py`          | Env-based provider selection (LLM, embeddings, Chroma path, CSV path) |
| `ingest.py`          | Load CSV, validate schema, chunk/index into Chroma with metadata      |
| `query_extractor.py` | LLM → structured filter object                                        |
| `retriever.py`       | Chroma query with metadata `where` clause + semantic search           |
| `recommender.py`     | LLM formats top results into friendly suggestions                     |
| `models.py`          | Pydantic request/response schemas                                     |
| `main.py`            | FastAPI routes and startup lifecycle                                  |


### Startup Lifecycle

1. Read `GAMES_CSV_PATH` (default: `./data/games.csv`)
2. Validate CSV schema
3. Hash file contents — skip re-index if unchanged
4. Index documents into Chroma with metadata
5. Expose `/health` and `/recommend` endpoints

---

## Data Model

### CSV Schema

Each row is one game. The bundled `data/games.csv` and any user-provided replacement must match this schema.


| Column              | Type   | Required | Description                     | Example                           |
| ------------------- | ------ | -------- | ------------------------------- | --------------------------------- |
| `name`              | string | yes      | Game title                      | `Catan`                           |
| `description`       | string | yes      | Short description for embedding | `Trade and build on an island...` |
| `min_players`       | int    | yes      | Minimum players                 | `3`                               |
| `max_players`       | int    | yes      | Maximum players                 | `4`                               |
| `play_time_minutes` | int    | yes      | Typical play time               | `90`                              |
| `categories`        | string | yes      | Comma-separated tags            | `strategy,economic`               |
| `complexity`        | string | no       | Difficulty level                | `medium`                          |


Allowed `complexity` values: `light`, `medium`, `heavy`.

### Chroma Document Mapping

- **Document text (embedded):** `{name}. {description}. Categories: {categories}.`
- **Metadata fields:** `name`, `min_players`, `max_players`, `play_time_minutes`, `categories` (stored as list), `complexity`

### Bundled Starter Dataset

Ship `data/games.csv` with ~25 curated games covering:

- Player counts: 1–8+
- Categories: strategy, party, family, abstract, cooperative, adventure, word, economic
- Play times: 15–120+ minutes
- Complexity: light, medium, heavy

Users can replace the dataset by setting `GAMES_CSV_PATH` to their own CSV file (same schema). Re-indexing happens automatically on file hash change.

---

## API Contract

### `POST /recommend`

**Request:**

```json
{
  "query": "Suggest a light strategy game for 4 players under 60 minutes"
}
```

**Response:**

```json
{
  "recommendations": [
    {
      "name": "Ticket to Ride",
      "reason": "Supports 2-5 players, ~60 min, strategy category fits your request.",
      "min_players": 2,
      "max_players": 5,
      "play_time_minutes": 60,
      "categories": ["strategy", "family"]
    }
  ],
  "reasoning": "Filtered 12 games down to 3 matches based on player count and play time.",
  "filters_applied": {
    "player_count": 4,
    "categories": ["strategy"],
    "max_play_time_minutes": 60,
    "complexity": "light"
  },
  "filters_relaxed": false
}
```

**Future multi-turn (v2-ready):** The request model includes an optional `session_id` field. Ignored in v1; reserved so v2 can add conversation memory without breaking clients.

### `GET /health`

```json
{
  "status": "ok",
  "indexed_games": 25
}
```

Returns `"status": "degraded"` if CSV is missing or indexing failed.

---

## Retrieval Logic

### Step 1: Query Extraction

The LLM extracts structured filters from natural language. Fields not mentioned in the query are left `null` — the system does not guess.

**Extracted filter schema:**

```python
{
  "player_count": int | null,       # exact player count requested
  "categories": list[str] | null,   # e.g. ["strategy", "party"]
  "max_play_time_minutes": int | null,
  "complexity": str | null,         # "light" | "medium" | "heavy"
  "keywords": list[str] | null      # free-form terms for semantic boost
}
```

Temperature: `0` (deterministic extraction).

### Step 2: Metadata Filtering

Build a Chroma `where` clause only from non-null fields:

- **Player count:** `min_players <= count AND max_players >= count`
- **Play time:** `play_time_minutes <= max_play_time_minutes`
- **Categories:** `$or` match — game matches if any requested category is present
- **Complexity:** exact match

### Step 3: Semantic Search

Run similarity search on filtered (or unfiltered) documents. Retrieve `top_k=5` candidates.

### Step 4: Fallback

If metadata filters return 0 results, retry with semantic-only search (no `where` clause). Set `"filters_relaxed": true` in the response.

### Step 5: Recommendation Synthesis

LLM receives top candidates and user query. Returns up to 3 recommendations with per-game reasons and overall reasoning.

Temperature: `0.3`.

---

## Provider Configuration

All providers selected via environment variables. **Recommended setup:** OpenRouter for both LLM and embeddings — one API key, no OpenAI account required.


| Variable                     | Purpose                                                    | Default                         |
| ---------------------------- | ---------------------------------------------------------- | ------------------------------- |
| `LLM_PROVIDER`               | `openrouter`, `openai`, or `ollama`                        | `openrouter`                    |
| `EMBEDDING_PROVIDER`         | `openrouter`, `openai`, or `ollama`                        | `openrouter`                    |
| `OPENROUTER_API_KEY`         | OpenRouter authentication (used for LLM and/or embeddings) | —                               |
| `OPENROUTER_MODEL`           | Chat model (OpenRouter model ID)                           | `openai/gpt-4o-mini`            |
| `OPENROUTER_EMBEDDING_MODEL` | Embedding model (OpenRouter model ID)                      | `openai/text-embedding-3-small` |
| `OPENAI_API_KEY`             | OpenAI authentication (when provider is `openai`)          | —                               |
| `OPENAI_MODEL`               | Chat model                                                 | `gpt-4o-mini`                   |
| `OPENAI_EMBEDDING_MODEL`     | Embedding model                                            | `text-embedding-3-small`        |
| `OLLAMA_BASE_URL`            | Ollama server URL                                          | `http://localhost:11434`        |
| `OLLAMA_MODEL`               | Chat model                                                 | `llama3`                        |
| `OLLAMA_EMBEDDING_MODEL`     | Embedding model                                            | `nomic-embed-text`              |
| `GAMES_CSV_PATH`             | Path to games CSV                                          | `./data/games.csv`              |
| `CHROMA_PERSIST_DIR`         | Chroma persistence directory                               | `./data/chroma`                 |


### OpenRouter (LLM + embeddings)

OpenRouter exposes an OpenAI-compatible API for both chat and embeddings (`POST /embeddings`).

**LLM** (`LLM_PROVIDER=openrouter`):

- LangChain `ChatOpenAI` with `base_url=https://openrouter.ai/api/v1`
- Model from `OPENROUTER_MODEL` (e.g. `openai/gpt-4o-mini`, `anthropic/claude-3-haiku`)

**Embeddings** (`EMBEDDING_PROVIDER=openrouter`):

- LangChain `OpenAIEmbeddings` with `base_url=https://openrouter.ai/api/v1`
- Model from `OPENROUTER_EMBEDDING_MODEL` (e.g. `openai/text-embedding-3-small`)
- Same `OPENROUTER_API_KEY` as the LLM

**Primary `.env` example (single key):**

```env
LLM_PROVIDER=openrouter
EMBEDDING_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=openai/gpt-4o-mini
OPENROUTER_EMBEDDING_MODEL=openai/text-embedding-3-small
```

Browse available embedding models: [https://openrouter.ai/models?output_modalities=embeddings](https://openrouter.ai/models?output_modalities=embeddings)

A factory in `config.py` instantiates the correct LangChain chat model and embedding model based on these vars.

---

## Error Handling


| Situation                          | HTTP Status    | Behavior                                               |
| ---------------------------------- | -------------- | ------------------------------------------------------ |
| Empty or missing query             | 422            | Pydantic validation error                              |
| CSV file missing or invalid schema | 503 on startup | Log error; `/health` returns `"degraded"`              |
| LLM provider unreachable           | 502            | `{ "error": "LLM unavailable" }`                       |
| No games indexed                   | 503            | `{ "error": "No games indexed" }`                      |
| Filters match 0 games              | 200            | Fallback to semantic search; `"filters_relaxed": true` |


---

## Project Structure

```
simple-langchain-app/l
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app + routes
│   ├── config.py            # Env-based provider config
│   ├── models.py            # Pydantic request/response schemas
│   ├── ingest.py            # CSV load, validate, Chroma index
│   ├── query_extractor.py   # LLM structured extraction
│   ├── retriever.py         # Chroma filtered retrieval
│   └── recommender.py       # LLM synthesis
├── data/
│   └── games.csv            # Bundled starter dataset (~25 games)
├── docs/
│   └── superpowers/specs/
│       └── 2026-06-22-boardgame-rag-design.md
├── tests/
│   ├── test_ingest.py
│   ├── test_retriever.py
│   └── test_api.py          # httpx TestClient, mocked LLM
├── .env.example
├── requirements.txt
└── README.md
```

---

## Testing Strategy

Minimal tests covering real behavior:


| Test                | What it verifies                                                            |
| ------------------- | --------------------------------------------------------------------------- |
| `test_ingest.py`    | CSV schema validation; rejects bad rows; indexes correct document count     |
| `test_retriever.py` | Player-count filter logic; category `$or` matching; fallback when 0 results |
| `test_api.py`       | `/recommend` returns 422 on empty query; response shape; LLM calls mocked   |


LLM and embedding calls are mocked in tests — no live API key required for CI.

---

## Tech Stack

- Python 3.11+
- FastAPI
- LangChain (chat models, embeddings, Chroma integration)
- Chroma (persistent vector store)
- pydantic-settings (config)
- httpx + pytest (testing)

---

## Out of Scope (v1)

- Multi-turn conversation / session memory
- CSV merge (append user data to bundled data)
- Web UI or CLI
- External API integration (BoardGameGeek)
- Authentication / rate limiting
- Docker deployment (documented in README as optional follow-up)

---

## Future Extensions (v2+)

- **Multi-turn:** Use `session_id` to maintain conversation history; refine recommendations across turns
- **CSV merge:** Combine bundled + user datasets during ingest
- **Re-index endpoint:** `POST /admin/reindex` to trigger re-ingest without restart
- **Streaming:** Stream recommendation text via SSE

