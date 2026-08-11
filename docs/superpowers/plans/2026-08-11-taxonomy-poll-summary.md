# Taxonomy FK + poll-summary arrays Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Normalize categories/mechanics to BGG-id lookup tables with FK junctions, and store poll-summary player counts as `INTEGER[]` on `games`, filled on crawl/recrawl after an option-1 schema cut.

**Architecture:** Parser emits `(id, name)` taxonomy tuples plus parsed `best_with_players` / `recommended_with_players` lists. ORM adds `Category` / `Mechanic` and rewrites junctions to FKs. Crawl upserts lookups and assigns arrays. Ingest still embeds category **names** only. Existing DBs run a one-shot SQL migration then reset `crawl_status` to `pending`.

**Tech Stack:** SQLAlchemy 2.x ORM, Postgres `INTEGER[]` (portable `IntArray` TypeDecorator for SQLite tests), existing BGG XML crawl, pytest + in-memory SQLite

**Spec:** `docs/superpowers/specs/2026-08-11-taxonomy-poll-summary-design.md`

## Global Constraints

- Category/mechanic PK = BGG link id; no `UNIQUE(name)`
- Junctions: `(game_id, category_id)` / `(game_id, mechanic_id)` only — drop string columns
- Poll arrays only — no raw poll-summary text; read BGG typo `recommmendedwith`
- Ignore full vote `<poll>` trees, player-age, language-dependence
- No GIN / reverse-lookup indexes in this change
- No Alembic — update `scripts/schema.sql` + ship `scripts/migrate_taxonomy_poll_summary.sql`
- RAG/Chroma: category names only; do not index poll arrays
- Option 1 migration: drop/recreate junctions, add arrays, reset crawl to pending (`crawl_attempts=0`)
- Prefer `tests/conftest.py` `db_session` (SQLite) for DB tests

---

## File map

| File | Responsibility |
|------|----------------|
| `app/bgg/parser.py` | Parse link ids + poll-summary → int lists |
| `tests/fixtures/bgg_thing.py` | Sample XML with poll-summary |
| `tests/test_bgg_parser.py` | Parser unit tests |
| `app/db/models.py` | `Category`, `Mechanic`, FK junctions, `IntArray` columns |
| `app/db/__init__.py` | Export new models |
| `scripts/schema.sql` | Bootstrap DDL matching ORM |
| `scripts/migrate_taxonomy_poll_summary.sql` | One-shot for existing Postgres DBs |
| `scripts/crawl_bgg_metadata.py` | Upsert lookups, write FKs + arrays |
| `tests/test_crawl_bgg_metadata.py` | Assert categories/mechanics ids + arrays |
| `app/ingest.py` | Map via `GameCategory.category.name` |
| `tests/test_ingest.py` | Seed with `Category` + FK |
| `tests/test_api.py` | Seed with `Category` + FK |

---

### Task 1: Parser — taxonomy ids + poll-summary arrays

**Files:**
- Modify: `app/bgg/parser.py`
- Modify: `tests/fixtures/bgg_thing.py`
- Modify: `tests/test_bgg_parser.py`

**Interfaces:**
- Consumes: BGG thing XML (`link`, `poll-summary`)
- Produces:
  - `BggThingData.categories: list[tuple[int, str]]`
  - `BggThingData.mechanics: list[tuple[int, str]]`
  - `BggThingData.best_with_players: list[int] | None`
  - `BggThingData.recommended_with_players: list[int] | None`
  - `parse_player_count_summary(value: str | None) -> list[int] | None`

- [ ] **Step 1: Write failing parser tests**

Update `tests/fixtures/bgg_thing.py` — insert poll-summary after minage (before links is also fine; mirror BGG order after playingtime if preferred). Replace `SAMPLE_THING_XML` body so it includes:

```xml
    <poll-summary name="suggested_numplayers" title="User Suggested Number of Players">
      <result name="bestwith" value="Best with 4–5 players" />
      <result name="recommmendedwith" value="Recommended with 3–6 players" />
    </poll-summary>
```

(Links already have `id` attributes.)

Replace / extend assertions in `tests/test_bgg_parser.py`:

```python
def test_parse_thing_response() -> None:
    parsed = parse_thing_response(SAMPLE_THING_XML)
    game = parsed[224517]
    assert game.categories == [(1021, "Economic"), (1086, "Territory Building")]
    assert game.mechanics == [(2081, "Route/Network Building")]
    assert game.best_with_players == [4, 5]
    assert game.recommended_with_players == [3, 4, 5, 6]


def test_parse_player_count_summary_variants() -> None:
    from app.bgg.parser import parse_player_count_summary

    assert parse_player_count_summary("Best with 4–5 players") == [4, 5]
    assert parse_player_count_summary("Best with 4-5 players") == [4, 5]
    assert parse_player_count_summary("Recommended with 2–4 players") == [2, 3, 4]
    assert parse_player_count_summary("Best with 2, 4 players") == [2, 4]
    assert parse_player_count_summary("Best with 4 players") == [4]
    assert parse_player_count_summary("Recommended with 7+ players") is None
    assert parse_player_count_summary("") is None
    assert parse_player_count_summary(None) is None


def test_parse_skips_links_missing_id() -> None:
    xml = """<?xml version="1.0"?>
    <items><item type="boardgame" id="1">
      <link type="boardgamecategory" value="Economic"/>
      <link type="boardgamecategory" id="1021" value="Economic"/>
    </item></items>"""
    game = parse_thing_response(xml)[1]
    assert game.categories == [(1021, "Economic")]
```

- [ ] **Step 2: Run tests — expect fail**

Run: `pytest tests/test_bgg_parser.py -v`

Expected: FAIL (missing `parse_player_count_summary` and/or wrong category shape)

- [ ] **Step 3: Implement parser**

In `app/bgg/parser.py`:

1. Change `BggThingData` fields as in Interfaces.
2. Add:

```python
_EN_DASH = "\u2013"
_RANGE_RE = re.compile(rf"(\d+)\s*(?:{_EN_DASH}|-)\s*(\d+)")


def parse_player_count_summary(value: str | None) -> list[int] | None:
    if not value or not value.strip():
        return None
    # Drop N+ tokens (e.g. "7+") — do not invent an open-ended count
    cleaned = re.sub(r"\d+\s*\+", " ", value)
    numbers: set[int] = set()
    for match in _RANGE_RE.finditer(cleaned):
        low, high = int(match.group(1)), int(match.group(2))
        if low < 1 or low > high:
            continue
        numbers.update(range(low, high + 1))
    without_ranges = _RANGE_RE.sub(" ", cleaned)
    for match in re.finditer(r"\d+", without_ranges):
        n = int(match.group(0))
        if n >= 1:
            numbers.add(n)
    if not numbers:
        return None
    return sorted(numbers)
```

`"Recommended with 7+ players"` → strip `7+` → no digits → `None`. `"Best with 2, 4 players"` → `[2, 4]`.

3. In `_parse_item`, for links require `id` + `value`:

```python
categories: list[tuple[int, str]] = []
mechanics: list[tuple[int, str]] = []
for link in item.findall("link"):
    link_type = link.attrib.get("type")
    value = link.attrib.get("value")
    raw_id = link.attrib.get("id")
    if not value or not raw_id:
        continue
    link_id = _int_or_none(raw_id)
    if link_id is None:
        continue
    if link_type == "boardgamecategory":
        categories.append((link_id, value))
    elif link_type == "boardgamemechanic":
        mechanics.append((link_id, value))

best_with = None
recommended_with = None
summary = item.find("poll-summary[@name='suggested_numplayers']")
if summary is not None:
    for result in summary.findall("result"):
        name = result.attrib.get("name")
        parsed = parse_player_count_summary(result.attrib.get("value"))
        if name == "bestwith":
            best_with = parsed
        elif name == "recommmendedwith":
            recommended_with = parsed
```

Pass the new fields into `BggThingData(...)`.

- [ ] **Step 4: Run tests — expect pass**

Run: `pytest tests/test_bgg_parser.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/bgg/parser.py tests/fixtures/bgg_thing.py tests/test_bgg_parser.py
git commit -m "$(cat <<'EOF'
feat: parse BGG taxonomy ids and poll-summary player arrays

EOF
)"
```

---

### Task 2: ORM models + schema.sql + migration SQL

**Files:**
- Modify: `app/db/models.py`
- Modify: `app/db/__init__.py`
- Modify: `scripts/schema.sql`
- Create: `scripts/migrate_taxonomy_poll_summary.sql`

**Interfaces:**
- Consumes: Task 1 types (not imported; crawl will wire later)
- Produces:
  - `Category(id, name)`, `Mechanic(id, name)`
  - `GameCategory(game_id, category_id)` + `category` relationship → `Category`
  - `GameMechanic(game_id, mechanic_id)` + `mechanic` relationship → `Mechanic`
  - `Game.best_with_players: list[int] | None`
  - `Game.recommended_with_players: list[int] | None`
  - `IntArray` TypeDecorator (Postgres `ARRAY(Integer)`, SQLite `JSON`)

- [ ] **Step 1: Write a failing model smoke test**

Add to `tests/test_crawl_bgg_metadata.py` (or new `tests/test_db_models_taxonomy.py`):

```python
def test_game_taxonomy_fk_and_arrays(db_session: Session) -> None:
    from app.db.models import Category, Game, GameCategory, Mechanic, GameMechanic

    db_session.add(Category(id=1021, name="Economic"))
    db_session.add(Mechanic(id=2081, name="Network Building"))
    game = Game(id=1, name="Test", is_expansion=False)
    game.best_with_players = [4, 5]
    game.recommended_with_players = [3, 4, 5, 6]
    game.categories.append(GameCategory(category_id=1021))
    game.mechanics.append(GameMechanic(mechanic_id=2081))
    db_session.add(game)
    db_session.commit()

    db_session.refresh(game)
    assert game.categories[0].category.name == "Economic"
    assert game.mechanics[0].mechanic.name == "Network Building"
    assert game.best_with_players == [4, 5]
    assert game.recommended_with_players == [3, 4, 5, 6]
```

- [ ] **Step 2: Run test — expect fail**

Run: `pytest tests/test_crawl_bgg_metadata.py::test_game_taxonomy_fk_and_arrays -v`  
(or the new file path)

Expected: FAIL (import/attribute errors)

- [ ] **Step 3: Implement models**

In `app/db/models.py`, add imports and `IntArray`:

```python
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    TypeDecorator,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY


class IntArray(TypeDecorator):
    """Postgres INTEGER[]; JSON list on SQLite for unit tests."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(ARRAY(Integer))
        return dialect.type_descriptor(JSON())
```

On `Game`, after `image_url`:

```python
    best_with_players: Mapped[list[int] | None] = mapped_column(IntArray)
    recommended_with_players: Mapped[list[int] | None] = mapped_column(IntArray)
```

Replace `GameCategory` / `GameMechanic` and add lookups:

```python
class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class Mechanic(Base):
    __tablename__ = "mechanics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class GameCategory(Base):
    __tablename__ = "game_categories"

    game_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("categories.id"), primary_key=True
    )

    game: Mapped[Game] = relationship(back_populates="categories")
    category: Mapped[Category] = relationship()


class GameMechanic(Base):
    __tablename__ = "game_mechanics"

    game_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    mechanic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("mechanics.id"), primary_key=True
    )

    game: Mapped[Game] = relationship(back_populates="mechanics")
    mechanic: Mapped[Mechanic] = relationship()
```

Update `app/db/__init__.py` to export `Category`, `Mechanic`.

Replace junction + add tables/columns in `scripts/schema.sql`:

- After `games` table definition, add the two array columns to the `CREATE TABLE games` body:

```sql
    best_with_players INTEGER[],
    recommended_with_players INTEGER[],
```

- Replace old junctions with categories/mechanics + new junctions (exact DDL from the spec).

Create `scripts/migrate_taxonomy_poll_summary.sql`:

```sql
-- One-shot for existing Postgres DBs (option 1). Run manually against the app DB.
BEGIN;

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS mechanics (
    id INTEGER PRIMARY KEY,
    name VARCHAR(255) NOT NULL
);

DROP TABLE IF EXISTS game_categories;
DROP TABLE IF EXISTS game_mechanics;

CREATE TABLE game_categories (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    PRIMARY KEY (game_id, category_id)
);

CREATE TABLE game_mechanics (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    mechanic_id INTEGER NOT NULL REFERENCES mechanics(id),
    PRIMARY KEY (game_id, mechanic_id)
);

ALTER TABLE games
    ADD COLUMN IF NOT EXISTS best_with_players INTEGER[],
    ADD COLUMN IF NOT EXISTS recommended_with_players INTEGER[];

UPDATE games
SET crawl_status = 'pending',
    crawled_at = NULL,
    crawl_attempts = 0,
    last_crawl_error = NULL;

COMMIT;
```

- [ ] **Step 4: Run smoke test — expect pass**

Run: `pytest tests/test_crawl_bgg_metadata.py::test_game_taxonomy_fk_and_arrays -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/db/models.py app/db/__init__.py scripts/schema.sql scripts/migrate_taxonomy_poll_summary.sql tests/test_crawl_bgg_metadata.py
git commit -m "$(cat <<'EOF'
feat: add taxonomy tables, FK junctions, and poll player arrays

EOF
)"
```

---

### Task 3: Crawl apply — upsert lookups + arrays

**Files:**
- Modify: `scripts/crawl_bgg_metadata.py`
- Modify: `tests/test_crawl_bgg_metadata.py`

**Interfaces:**
- Consumes: `BggThingData` from Task 1; `Category`, `Mechanic`, `GameCategory`, `GameMechanic` from Task 2
- Produces: `_apply_thing_data` upserts taxonomy, rebuilds FK junctions, sets array columns

- [ ] **Step 1: Extend crawl integration test**

In `test_crawl_marks_games_completed`, after refresh:

```python
    assert {(c.category_id, c.category.name) for c in game.categories} == {
        (1021, "Economic"),
        (1086, "Territory Building"),
    }
    assert {(m.mechanic_id, m.mechanic.name) for m in game.mechanics} == {
        (2081, "Route/Network Building"),
    }
    assert game.best_with_players == [4, 5]
    assert game.recommended_with_players == [3, 4, 5, 6]
```

(Requires Task 1 fixture poll-summary present.)

- [ ] **Step 2: Run test — expect fail**

Run: `pytest tests/test_crawl_bgg_metadata.py::test_crawl_marks_games_completed -v`

Expected: FAIL on category/array assertions (old string apply path)

- [ ] **Step 3: Implement `_apply_thing_data`**

Update imports:

```python
from app.db.models import (
    Category,
    CrawlStatus,
    Game,
    GameCategory,
    GameMechanic,
    Mechanic,
)
```

Replace taxonomy section of `_apply_thing_data`:

```python
    game.categories.clear()
    for category_id, category_name in thing_data.categories:
        category = session.get(Category, category_id)
        if category is None:
            category = Category(id=category_id, name=category_name)
            session.add(category)
        elif category.name != category_name:
            category.name = category_name
        game.categories.append(GameCategory(category_id=category_id))

    game.mechanics.clear()
    for mechanic_id, mechanic_name in thing_data.mechanics:
        mechanic = session.get(Mechanic, mechanic_id)
        if mechanic is None:
            mechanic = Mechanic(id=mechanic_id, name=mechanic_name)
            session.add(mechanic)
        elif mechanic.name != mechanic_name:
            mechanic.name = mechanic_name
        game.mechanics.append(GameMechanic(mechanic_id=mechanic_id))

    game.best_with_players = thing_data.best_with_players
    game.recommended_with_players = thing_data.recommended_with_players
```

Flush lookups before appending junctions if SQLite complains about missing FK parents (`session.flush()` after the upsert loops, or flush once before append). Prefer: upsert all categories/mechanics first with `session.flush()`, then clear/rebuild junctions.

- [ ] **Step 4: Run crawl tests — expect pass**

Run: `pytest tests/test_crawl_bgg_metadata.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/crawl_bgg_metadata.py tests/test_crawl_bgg_metadata.py
git commit -m "$(cat <<'EOF'
feat: crawl upserts taxonomy FKs and poll-summary arrays

EOF
)"
```

---

### Task 4: Ingest + API/test seed updates

**Files:**
- Modify: `app/ingest.py`
- Modify: `tests/test_ingest.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: `GameCategory.category.name`
- Produces: unchanged RAG row shape (`categories` string); poll arrays unused

- [ ] **Step 1: Update failing seed helpers / ingest mapping test**

In `tests/test_ingest.py` `_seed_eligible`:

```python
from app.db.models import Category, CrawlStatus, Game, GameCategory

    ...
    db_session.add(Category(id=1021, name="Economic"))
    db_session.flush()
    game.categories.append(GameCategory(category_id=1021))
```

Same pattern in `tests/test_api.py` for Strategy — use a stable fake id (e.g. `1000`, name `"Strategy"`).

Update `app/ingest.py` `_eligible_games_stmt` to eager-load category names:

```python
from sqlalchemy.orm import selectinload
from app.db.models import CrawlStatus, Game, GameCategory

        .options(
            selectinload(Game.categories).selectinload(GameCategory.category),
        )
```

Update `_game_to_row`:

```python
    categories = ",".join(
        link.category.name.lower().replace(" ", "_") for link in game.categories
    )
```

- [ ] **Step 2: Run ingest + api tests — expect fail before mapping fix if seeds already updated**

Run: `pytest tests/test_ingest.py tests/test_api.py -v`

Expected: FAIL until mapping/seeds updated; then proceed to Step 3 if not already done.

- [ ] **Step 3: Apply ingest + seed changes above**

- [ ] **Step 4: Run full relevant suite**

Run:

```bash
pytest tests/test_bgg_parser.py tests/test_crawl_bgg_metadata.py tests/test_ingest.py tests/test_api.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/ingest.py tests/test_ingest.py tests/test_api.py
git commit -m "$(cat <<'EOF'
fix: map RAG categories via taxonomy FK relationship

EOF
)"
```

---

### Task 5: Full regression + operator notes

**Files:**
- Modify only if docs already mention old junction shape: `docs/database.md` (if present) — one short note pointing at `scripts/migrate_taxonomy_poll_summary.sql` and recrawl
- Otherwise skip doc edits beyond what exists

- [ ] **Step 1: Run full test suite**

Run: `pytest -v`

Expected: PASS

- [ ] **Step 2: Document operator steps in commit message / ensure migration script header is clear**

Confirm `scripts/migrate_taxonomy_poll_summary.sql` header comments say:

1. Apply SQL against Postgres
2. Restart not enough — must run crawler (`scripts/crawl_bgg_metadata.py`)
3. Fresh installs use updated `schema.sql` + import + crawl (no migrate script needed)

If `docs/database.md` exists and describes schema, add a short “Taxonomy migration” bullet with those three steps; if the file does not exist, skip.

- [ ] **Step 3: Commit only if docs changed**

```bash
git add docs/database.md scripts/migrate_taxonomy_poll_summary.sql
git commit -m "$(cat <<'EOF'
docs: note taxonomy migration and required recrawl

EOF
)"
```

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|------------------|------|
| `categories` / `mechanics` tables (BGG id PK) | Task 2 |
| FK junctions; drop string columns | Task 2 |
| `best_with_players` / `recommended_with_players` arrays | Task 2–3 |
| Option 1 migrate + crawl reset | Task 2 (`migrate_*.sql`) |
| Parser ids + poll-summary (incl. typo key) | Task 1 |
| Range / list / single / `None` / `N+` | Task 1 |
| Crawl upsert + arrays | Task 3 |
| Ingest uses category names; no poll in Chroma | Task 4 |
| No Alembic / no GIN / no full polls | Honored (out of scope) |
| Fixture + test updates | Tasks 1, 3, 4 |

No TBD placeholders. Type names consistent: `Category`, `Mechanic`, `category_id`, `mechanic_id`, `best_with_players`, `recommended_with_players`, `parse_player_count_summary`.
