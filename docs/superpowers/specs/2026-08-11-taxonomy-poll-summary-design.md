# Taxonomy FK + poll-summary arrays

Date: 2026-08-11  
Status: approved design (pending implementation plan)

## Goal

Normalize BGG categories and mechanics into lookup tables keyed by BGG id, with junction FKs. On crawl/recrawl, also store suggested player-count poll-summary as integer arrays on `games`. Force existing rows through recrawl via option 1 (schema cut + `crawl_status=pending`).

## Decisions

| Topic | Choice |
|-------|--------|
| Category/mechanic identity | BGG link `id` as PK; `name` stored on lookup table |
| Junction shape | `(game_id, category_id)` / `(game_id, mechanic_id)` FKs; drop string columns |
| Migration | Option 1: recreate junctions, add array columns, reset crawl to pending |
| Poll-summary storage | Parsed `INT[]` only — no original text |
| Non-contiguous sets | Preserve as arrays (e.g. `2, 4` → `{2,4}`) |
| BGG typo | Read `recommmendedwith` as recommended |
| Full vote `<poll>` | Out of scope |
| Extra indexes / GIN | Out of scope until query paths need them |
| RAG / Chroma | Keep category **names** in metadata; do not index poll arrays yet |
| Migrations tool | Update `scripts/schema.sql` + ORM; one-shot SQL for existing DBs; no Alembic |

## Schema

### New tables

```sql
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,  -- BGG boardgamecategory link id
    name VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS mechanics (
    id INTEGER PRIMARY KEY,  -- BGG boardgamemechanic link id
    name VARCHAR(255) NOT NULL
);
```

No `UNIQUE(name)` — BGG id is the source of truth; names may be updated on recrawl.

### Junctions (replace existing)

```sql
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
```

### Games columns (add)

```sql
ALTER TABLE games
    ADD COLUMN IF NOT EXISTS best_with_players INTEGER[],
    ADD COLUMN IF NOT EXISTS recommended_with_players INTEGER[];
```

### Indexes

- Keep `idx_games_crawl_status`, `idx_games_rank`.
- Junction PKs provide game→taxonomy access.
- Do not add `category_id`-only indexes or array GIN indexes in this change.

## Migration (option 1)

For an existing populated database:

1. Create `categories` and `mechanics` if missing.
2. Drop `game_categories` and `game_mechanics` (string PKs are not migratable to FKs without BGG ids).
3. Recreate junctions with the FK shape above.
4. Add `best_with_players` and `recommended_with_players` to `games`.
5. Reset crawl state so every game is re-fetched:

```sql
UPDATE games
SET crawl_status = 'pending',
    crawled_at = NULL,
    crawl_attempts = 0,
    last_crawl_error = NULL;
```

Core `games` rows (name, ranks, dump fields, etc.) are retained. Associations and poll arrays are empty/null until recrawl.

For fresh/dev setups: apply updated `scripts/schema.sql`, re-import dump, crawl.

Until recrawl finishes, pending games remain ineligible for RAG ingest. Completed games with empty categories still default to `"strategy"` in ingest mapping (unchanged policy).

## Parser

File: `app/bgg/parser.py`

### `BggThingData` shape

- `categories: list[tuple[int, str]]` — `(bgg_id, name)`
- `mechanics: list[tuple[int, str]]`
- `best_with_players: list[int] | None`
- `recommended_with_players: list[int] | None`

### Links

For `boardgamecategory` / `boardgamemechanic` links, require both `id` and `value`. Skip the link if either is missing.

### Poll-summary

From `<poll-summary name="suggested_numplayers">`:

| XML `result/@name` | Field |
|--------------------|--------|
| `bestwith` | `best_with_players` |
| `recommmendedwith` | `recommended_with_players` (BGG spelling) |

Parse `value` text into a sorted list of unique positive integers:

| Example value | Result |
|---------------|--------|
| `Best with 4–5 players` / hyphen `-` | `[4, 5]` |
| `Recommended with 2–4 players` | `[2, 3, 4]` |
| `Best with 2, 4 players` | `[2, 4]` |
| Single number (e.g. `Best with 4 players`) | `[4]` |
| Missing node, empty, or unparseable | `None` |

Rules:

- Expand continuous ranges inclusively.
- Split comma-separated numbers without filling gaps.
- Do not invent counts from `N+` suffixes; if the only signal is unparseable (including bare `7+`), return `None` for that field.
- Ignore full `<poll name="suggested_numplayers">` vote trees, player-age, and language-dependence polls.

## Crawl apply

File: `scripts/crawl_bgg_metadata.py` (`_apply_thing_data`)

1. Upsert each category/mechanic by id (`merge` / get-or-create); update `name` when BGG renames.
2. Clear and rebuild `game.categories` / `game.mechanics` as FK junction rows.
3. Assign `game.best_with_players` and `game.recommended_with_players` from parser lists (`None` clears / leaves null).

ORM:

- `Category`, `Mechanic` models.
- `GameCategory.category_id` FK; relationship to `Category` for name access.
- `GameMechanic.mechanic_id` FK; relationship to `Mechanic`.
- `Game.best_with_players` / `recommended_with_players` as `ARRAY(Integer)` (portable enough for Postgres prod and SQLite unit tests via SQLAlchemy array handling / TypeDecorator as needed in implementation).

## RAG ingest impact

File: `app/ingest.py`

- Load categories via join/relationship to `Category.name` (same lowercasing and spaces→underscores as today).
- Do **not** add poll arrays to Chroma document text or metadata in this change.
- Retriever and `/recommend` filter schema unchanged.

## Out of scope

- Storing raw poll-summary strings
- Per-player-count Best/Recommended/Not Recommended vote rows
- `suggested_playerage` / `language_dependence` polls
- Alembic
- GIN or reverse-lookup indexes on junctions/arrays
- Using poll arrays in recommendation filters
- Wiping or re-importing the `games` dump table

## Testing

- Parser: link id+name extraction; poll-summary range, list, single, `None`; `recommmendedwith` key
- Crawl apply: lookup upsert, FK junctions, array columns on `Game`
- Ingest: category name mapping after relationship change
- Update fixtures (`tests/fixtures/bgg_thing.py`) to include `id` on links and a `poll-summary` block
- Adjust model constructors in API/ingest/crawl tests

## Data flow (after change)

```text
import dump → games (pending)
                ↓
         crawl thing XML
                ↓
    upsert categories/mechanics
    write game_* FK junctions
    set best_with / recommended_with arrays
    mark completed
                ↓
         ingest eligible games → Chroma (category names only, as today)
```
