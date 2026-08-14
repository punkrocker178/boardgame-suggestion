# Catalog search and autocomplete

Date: 2026-08-14  
Status: approved design (pending implementation plan)

## Goal

Add a SQL catalog search API that reuses hard filters from `app/sql_filters.py`, supports optional fuzzy name search with cursor pagination, and a separate name autocomplete endpoint. No LLM or Chroma.

## Decisions

| Topic | Choice |
|-------|--------|
| Routes | `POST /search` (JSON body) and `GET /search/autocomplete` |
| Name match (search) | Postgres `pg_trgm` (`similarity` ≥ 0.3); SQLite tests use `ILIKE '%q%'` |
| Name match (autocomplete) | Prefix (`q%`) first, then substring (`%q%`); no trigram |
| Catalog scope | All `games` rows (expansions and incomplete crawls included) |
| Eligibility | Do **not** apply `_eligible_games_filters` |
| Pagination | Cursor on `/search` only (`limit` + `cursor`) |
| Empty `q` | Allowed on search: browse by `rank ASC NULLS LAST, id ASC` |
| Autocomplete `q` | Required, min 2 characters |
| `keywords` | Ignored; name search is `q` only |
| Totals | No `total` count in the response |
| Indexing/LLM down | Search still returns 200 |

## API

### `POST /search`

JSON body fields:

- Filter fields matching `ExtractedFilters` / `HARD_FILTER_FIELDS`: `player_count`, `categories`, `max_play_time_minutes`, `complexity`, `min_weight`, `max_weight`, `min_age`, `max_age`, `min_year`, `max_year`, `best_with_player_count`, `recommended_with_player_count`.
- `q`: optional string.
- `limit`: default 20, max 50, min 1.
- `cursor`: optional opaque string from the previous page.

Filters are applied via `build_filter_predicates`. The client must resend the same `q` and filters on every page; the cursor is position only.

With `q`: Postgres uses `name % :q` / `similarity(name, :q) >= 0.3`, ordered `similarity DESC, rank ASC NULLS LAST, id ASC`.

Without `q`: ordered `rank ASC NULLS LAST, id ASC`.

Response:

```json
{
  "items": [
    {
      "id": 13,
      "name": "Catan",
      "year_published": 1995,
      "rank": 400,
      "is_expansion": false,
      "min_players": 3,
      "max_players": 4,
      "playing_time": 90,
      "min_age": 10,
      "weight": 2.3,
      "thumbnail_url": "...",
      "categories": ["Strategy"]
    }
  ],
  "next_cursor": "eyJ..."
}
```

`next_cursor` is `null` on the last page. Fetch `limit + 1` rows; if an extra row exists, encode a cursor from the last returned item (not the extra row).

### `GET /search/autocomplete`

Query params: `q` (required, min length 2), `limit` (default 10, max 20).

No filters, no cursor.

Where: `name ILIKE '%' || q || '%'`. Order: prefix matches first (`ILIKE q || '%'`), then `rank ASC NULLS LAST`, `id ASC`.

Response:

```json
{
  "suggestions": [
    { "id": 13, "name": "Catan", "year_published": 1995 }
  ]
}
```

## Architecture

New `app/search.py`. Routes in `app/main.py` stay thin. Do not fold this into `fetch_candidate_ids` or SQL relaxation.

Units:

1. Pydantic models in `app/models.py`: `SearchRequest`, `SearchGame`, `SearchResponse`, autocomplete request/response.
2. `search_games(session, request)` — `select(Game)` with `selectinload` of categories, predicates, name match, cursor decode, `limit+1`.
3. `autocomplete_games(session, q, limit)` — prefix-then-substring list.
4. Dialect-aware name match: `pg_trgm` on PostgreSQL; `ILIKE` substring on SQLite.
5. Cursor encode/decode: URL-safe base64 JSON. With `q`: `{ "sim", "rank", "id" }`. Browse: `{ "rank", "id" }`. Invalid or tampered cursor → 400 `{ "error": "Invalid cursor" }`.

Does not depend on Chroma, LLM, or indexing.

## Cursor comparison

Treat `rank` nulls as last (same as `NULLS LAST`).

Browse (rank ASC, id ASC), last keys `(rank, id)`:

- If last `rank` is a number: next rows satisfy `rank > last_rank` OR (`rank = last_rank` AND `id > last_id`) OR `rank IS NULL`.
- If last `rank` is null: `rank IS NULL AND id > last_id`.

With `q` (similarity DESC, then same rank/id):

- `sim < last_sim` OR (`sim = last_sim` AND browse-after `(rank, id)`).

Similarity in the cursor is the Postgres `similarity()` value (SQLite tests may use a constant `1.0` for ILIKE hits so pagination still advances by `rank, id`).

## Schema

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_games_name_trgm ON games USING gin (name gin_trgm_ops);
```

Update `scripts/schema.sql` and add a one-shot migrate script for existing DBs (same pattern as taxonomy: no Alembic). SQLite tests skip the extension and GIN index.

## Errors

| Case | Status |
|------|--------|
| `limit` out of range, autocomplete `q` shorter than 2 chars | 422 |
| Invalid/tampered `cursor` | 400 `{ "error": "Invalid cursor" }` |
| Zero matches | 200 empty list, `next_cursor: null` |
| Indexing/LLM down | search still 200 |

## Tests

`tests/test_search.py` with SQLite + `TestClient` (same pattern as `tests/test_api.py`):

- Filters-only browse includes expansions and pending crawls.
- Each `HARD_FILTER_FIELDS` path narrows via `build_filter_predicates`.
- `q` matches substring on SQLite (stand-in for trigram).
- Cursor: page 1 and page 2 do not overlap; last page has `next_cursor: null`.
- Bad cursor → 400.
- Autocomplete: `"Cat"` prefers a prefix name over a later substring; `q=a` → 422.
- `/search` does not require a Chroma index.

## Out of scope

- Mechanics filters
- Semantic / Chroma search on this path
- Filter relaxation
- Returning `total`
- Frontend UI
