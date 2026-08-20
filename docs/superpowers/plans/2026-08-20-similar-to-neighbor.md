# Similar-to Neighbor Ranking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rank `/recommend` hits as Chroma neighbors of a named seed game, with mechanics in ingest text, while keeping existing SQL hard filters.

**Architecture:** LLM sets `similar_to`. `lookup_indexed_game_by_name` resolves that name on eligible games (`pg_trgm` on Postgres, `ILIKE` on SQLite). On hit, Chroma is queried with rebuilt `_document_text` (name, description, categories, mechanics) and the seed `game_id` is dropped. On miss, today’s user-query embedding is used. Catalog search is not built; it must later import the same name-match helpers.

**Tech Stack:** FastAPI, SQLAlchemy, Chroma, LangChain structured extract, pytest + in-memory SQLite, Postgres `pg_trgm` in production schema

**Spec:** `docs/superpowers/specs/2026-08-20-similar-to-neighbor-design.md`

## Global Constraints

- `similar_to` is a seed name, not a SQL predicate; do not add it to `HARD_FILTER_FIELDS`
- Extraction this pass is LLM-only; do not implement text-extractor like-phrases
- Name lookup uses `_eligible_games_filters()` (same as ingest); expansions / pending crawls cannot be seeds
- Postgres: `name % :q` AND `similarity(name, :q) >= 0.3`, order `similarity DESC, rank ASC NULLS LAST, id ASC`, limit 1
- SQLite tests: `ILIKE '%q%'`, order `rank ASC NULLS LAST, id ASC`, limit 1; never ILIKE as a Postgres fallback
- Watermark stays `{count}:{stamp}`; do not bust cache in code (operator force-reindexes)
- Chroma metadata unchanged; mechanics only in `page_content`
- Empty `similar_to` string → null
- Lookup miss is not 4xx / 502; log at info; keep `similar_to` on `filters_applied`
- After dropping the seed, if the hit list is empty, continue SQL relaxation (do not return `[]` early)
- No new dependencies
- If this tree has no git repo, skip commit steps

---

## File map

| File | Responsibility |
|------|----------------|
| `app/models.py` | `similar_to` on `ExtractedFilters` and `FiltersApplied` |
| `app/name_match.py` | Dialect-aware name predicate/order + `lookup_indexed_game_by_name` |
| `app/ingest.py` | Mechanics in `_game_to_row` / `_document_text`; selectinload mechanics |
| `app/retriever.py` | `resolve_seed_query`; drop seed id; `top_k+1` fetch when excluding |
| `app/query_extractor.py` | LLM prompt field for `similar_to` |
| `app/recommender.py` | Copy `similar_to` into `FiltersApplied` |
| `scripts/schema.sql` | `pg_trgm` extension + GIN index |
| `scripts/migrate_pg_trgm.sql` | One-shot for existing Postgres volumes |
| `docs/database.md` | Operator note for the migrate script |
| `tests/test_llm_parsing.py` | Schema / empty-string tests |
| `tests/test_name_match.py` | SQLite lookup tests |
| `tests/test_ingest.py` | Mechanics in rows/docs |
| `tests/test_retriever.py` | Seed query + exclude seed |
| `tests/test_sql_filters.py` | `similar_to` is not a hard filter |
| `tests/test_api.py` | `filters_applied.similar_to` |

---

### Task 1: `similar_to` on filter models

**Files:**
- Modify: `app/models.py`
- Test: `tests/test_llm_parsing.py`

**Interfaces:**
- Consumes: existing `ExtractedFilters.normalize_loose_schema`
- Produces: `ExtractedFilters.similar_to: str | None`, `FiltersApplied.similar_to: str | None`; `""` / whitespace-only → `None`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_llm_parsing.py`:

```python
def test_extracted_filters_similar_to_round_trip() -> None:
    filters = ExtractedFilters.model_validate({"similar_to": "Catan"})
    assert filters.similar_to == "Catan"


def test_extracted_filters_blank_similar_to_is_none() -> None:
    filters = ExtractedFilters.model_validate({"similar_to": "  "})
    assert filters.similar_to is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_llm_parsing.py::test_extracted_filters_similar_to_round_trip tests/test_llm_parsing.py::test_extracted_filters_blank_similar_to_is_none -v`

Expected: FAIL (`similar_to` extra / ignored / not None)

- [ ] **Step 3: Minimal implementation**

On `FiltersApplied` and `ExtractedFilters`, add:

```python
similar_to: str | None = None
```

In `ExtractedFilters.normalize_loose_schema`, before `return data`:

```python
similar_to = data.get("similar_to")
if isinstance(similar_to, str) and not similar_to.strip():
    data["similar_to"] = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_llm_parsing.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/models.py tests/test_llm_parsing.py
git commit -m "$(cat <<'EOF'
feat: add similar_to on extracted filters

EOF
)"
```

---

### Task 2: `pg_trgm` schema (Postgres only)

**Files:**
- Modify: `scripts/schema.sql`
- Create: `scripts/migrate_pg_trgm.sql`
- Modify: `docs/database.md`

**Interfaces:**
- Consumes: existing bootstrap DDL
- Produces: `CREATE EXTENSION IF NOT EXISTS pg_trgm;` and `idx_games_name_trgm` on `games.name`; one-shot migrate for existing volumes

- [ ] **Step 1: Add extension + index to `scripts/schema.sql`**

Immediately after the file’s header comment, add:

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

After the existing `idx_games_rank` line, add:

```sql
CREATE INDEX IF NOT EXISTS idx_games_name_trgm ON games USING gin (name gin_trgm_ops);
```

- [ ] **Step 2: Create `scripts/migrate_pg_trgm.sql`**

```sql
-- One-shot for existing Postgres DBs. Run manually against the app DB.
-- Fresh installs: use updated scripts/schema.sql (skip this file).
-- SQLite tests do not run this file.
BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_games_name_trgm ON games USING gin (name gin_trgm_ops);

COMMIT;
```

- [ ] **Step 3: Document in `docs/database.md`**

After the “Taxonomy FK + poll-summary migration” subsection, add:

```markdown
### pg_trgm name index (existing DBs)

Needed for similar-to name lookup (and later catalog search):

1. Apply `scripts/migrate_pg_trgm.sql`.
2. Fresh installs: updated `scripts/schema.sql` already creates the extension and GIN index; skip this migrate script.
```

- [ ] **Step 4: Commit**

```bash
git add scripts/schema.sql scripts/migrate_pg_trgm.sql docs/database.md
git commit -m "$(cat <<'EOF'
feat: enable pg_trgm for game name lookup

EOF
)"
```

---

### Task 3: `lookup_indexed_game_by_name`

**Files:**
- Create: `app/name_match.py`
- Create: `tests/test_name_match.py`

**Interfaces:**
- Consumes: `Session`, `Game`, `_eligible_games_filters` from `app.ingest`
- Produces:
  - `PG_TRGM_MIN_SIMILARITY = 0.3`
  - `name_match_predicate(session: Session, q: str)`
  - `name_match_order(session: Session, q: str)` — tuple of SQLAlchemy order clauses
  - `lookup_indexed_game_by_name(session: Session, q: str) -> Game | None`
- Lookup `selectinload`s categories and mechanics so `_game_to_row` can run without extra queries
- Blank `q` → `None` without querying
- Catalog search (future) must import these helpers; do not duplicate the dialect split

- [ ] **Step 1: Write the failing tests**

Create `tests/test_name_match.py`:

```python
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models import Category, CrawlStatus, Game, GameCategory
from app.name_match import lookup_indexed_game_by_name


def _eligible(
    session: Session,
    *,
    game_id: int,
    name: str,
    rank: int,
    is_expansion: bool = False,
    status: str = CrawlStatus.COMPLETED,
) -> Game:
    game = Game(
        id=game_id,
        name=name,
        rank=rank,
        is_expansion=is_expansion,
        crawl_status=status,
        description="x",
        min_players=2,
        max_players=4,
        playing_time=60,
        crawled_at=datetime.now(UTC),
    )
    session.add(game)
    return game


def test_lookup_top_hit_by_rank(db_session: Session) -> None:
    db_session.add(Category(id=1, name="Strategy"))
    db_session.flush()
    junior = _eligible(db_session, game_id=1, name="Catan Junior", rank=1)
    junior.categories.append(GameCategory(category_id=1))
    catan = _eligible(db_session, game_id=2, name="Catan", rank=50)
    catan.categories.append(GameCategory(category_id=1))
    db_session.commit()

    hit = lookup_indexed_game_by_name(db_session, "Catan")
    assert hit is not None
    assert hit.id == 1
    assert hit.name == "Catan Junior"


def test_lookup_miss(db_session: Session) -> None:
    assert lookup_indexed_game_by_name(db_session, "NoSuchGame") is None
    assert lookup_indexed_game_by_name(db_session, "  ") is None


def test_lookup_skips_expansion(db_session: Session) -> None:
    db_session.add(Category(id=1, name="Strategy"))
    db_session.flush()
    exp = _eligible(
        db_session, game_id=9, name="Catan", rank=1, is_expansion=True
    )
    exp.categories.append(GameCategory(category_id=1))
    db_session.commit()
    assert lookup_indexed_game_by_name(db_session, "Catan") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_name_match.py -v`

Expected: FAIL (`app.name_match` not found)

- [ ] **Step 3: Minimal implementation**

Create `app/name_match.py`:

```python
from sqlalchemy import func, select, true
from sqlalchemy.orm import Session, selectinload

from app.db.models import Game, GameCategory, GameMechanic
from app.ingest import _eligible_games_filters

PG_TRGM_MIN_SIMILARITY = 0.3


def _dialect_name(session: Session) -> str:
    return session.get_bind().dialect.name


def name_match_predicate(session: Session, q: str):
    if _dialect_name(session) == "postgresql":
        return Game.name.op("%")(q)
    return Game.name.ilike(f"%{q}%")


def name_match_order(session: Session, q: str):
    if _dialect_name(session) == "postgresql":
        return (
            func.similarity(Game.name, q).desc(),
            Game.rank.asc().nulls_last(),
            Game.id.asc(),
        )
    return (Game.rank.asc().nulls_last(), Game.id.asc())


def lookup_indexed_game_by_name(session: Session, q: str) -> Game | None:
    needle = (q or "").strip()
    if not needle:
        return None

    filters = [*_eligible_games_filters(), name_match_predicate(session, needle)]
    if _dialect_name(session) == "postgresql":
        filters.append(func.similarity(Game.name, needle) >= PG_TRGM_MIN_SIMILARITY)
    else:
        filters.append(true())

    stmt = (
        select(Game)
        .options(
            selectinload(Game.categories).selectinload(GameCategory.category),
            selectinload(Game.mechanics).selectinload(GameMechanic.mechanic),
        )
        .where(*filters)
        .order_by(*name_match_order(session, needle))
        .limit(1)
    )
    return session.scalars(stmt).first()
```

Do not add a Postgres → ILIKE fallback.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_name_match.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/name_match.py tests/test_name_match.py
git commit -m "$(cat <<'EOF'
feat: look up eligible games by fuzzy name

EOF
)"
```

---

### Task 4: Mechanics in Chroma document text

**Files:**
- Modify: `app/ingest.py` (`_document_text`, `_game_to_row`, `_eligible_games_stmt`)
- Modify: `tests/test_ingest.py`

**Interfaces:**
- Consumes: `Game.mechanics` / `GameMechanic.mechanic`
- Produces: row key `mechanics` (comma-joined, lowercase, spaces → underscores); `_document_text` appends ` Mechanics: {mechanics}.` only when that string is non-empty
- Default category `"strategy"` when categories are empty stays; do not default mechanics
- Watermark and Chroma metadata unchanged

- [ ] **Step 1: Write the failing tests**

In `tests/test_ingest.py`:

1. Import `GameMechanic`, `Mechanic`.
2. In `_seed_eligible`, after adding the category, add a mechanic and junction:

```python
session.add(Mechanic(id=2081, name="Network Building"))
session.flush()
game.mechanics.append(GameMechanic(mechanic_id=2081))
```

3. In `test_load_games_for_rag_filters_and_formats`, assert:

```python
assert rows[0]["mechanics"] == "network_building"
```

4. Add:

```python
def test_rows_to_documents_includes_mechanics() -> None:
    rows = [
        {
            "id": "42",
            "name": "Catan",
            "description": "Trade and build",
            "min_players": "3",
            "max_players": "4",
            "play_time_minutes": "90",
            "categories": "strategy",
            "mechanics": "hexagon_grid,dice_rolling",
        }
    ]
    docs = rows_to_documents(rows)
    assert "Mechanics: hexagon_grid,dice_rolling." in docs[0].page_content
    assert "mechanics" not in docs[0].metadata


def test_rows_to_documents_omits_mechanics_clause_when_empty() -> None:
    rows = [
        {
            "id": "1",
            "name": "X",
            "description": "Y",
            "min_players": "2",
            "max_players": "4",
            "play_time_minutes": "30",
            "categories": "strategy",
        }
    ]
    docs = rows_to_documents(rows)
    assert "Mechanics:" not in docs[0].page_content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest.py::test_load_games_for_rag_filters_and_formats tests/test_ingest.py::test_rows_to_documents_includes_mechanics tests/test_ingest.py::test_rows_to_documents_omits_mechanics_clause_when_empty -v`

Expected: FAIL (missing `mechanics` / missing `Mechanics:` in text)

- [ ] **Step 3: Minimal implementation**

Import `GameMechanic` next to `GameCategory`.

`_eligible_games_stmt` options:

```python
.options(
    selectinload(Game.categories).selectinload(GameCategory.category),
    selectinload(Game.mechanics).selectinload(GameMechanic.mechanic),
)
```

In `_game_to_row`, after categories:

```python
mechanics = ",".join(
    link.mechanic.name.lower().replace(" ", "_") for link in game.mechanics
)
```

Add `"mechanics": mechanics` to `row` (empty string is fine; `_document_text` omits the clause).

Replace `_document_text`:

```python
def _document_text(row: dict[str, str]) -> str:
    text = f"{row['name']}. {row['description']}. Categories: {row['categories']}."
    mechanics = (row.get("mechanics") or "").strip()
    if mechanics:
        text += f" Mechanics: {mechanics}."
    return text
```

Do not put `mechanics` on Chroma metadata in `rows_to_documents`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ingest.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/ingest.py tests/test_ingest.py
git commit -m "$(cat <<'EOF'
feat: embed mechanics in Chroma document text

EOF
)"
```

---

### Task 5: Seed document query + drop seed from hits

**Files:**
- Modify: `app/retriever.py`
- Modify: `tests/test_retriever.py`
- Modify: `tests/test_sql_filters.py`

**Interfaces:**
- Consumes: `lookup_indexed_game_by_name`, `_document_text`, `_game_to_row`
- Produces:
  - `resolve_seed_query(session, filters, user_query) -> tuple[str, int | None]`
  - `retrieve_games` uses that query; fetches `top_k + 1` when `exclude_id` is set; drops matching `game_id`; if nothing left, relaxes further

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_sql_filters.py` in `test_has_active_hard_filters`:

```python
assert has_active_hard_filters(ExtractedFilters(similar_to="Catan")) is False
```

Add to `tests/test_retriever.py`:

```python
from app.ingest import _document_text, _game_to_row
from app.retriever import resolve_seed_query


def test_resolve_seed_query_hit_uses_document_text(
    seeded_session: Session,
) -> None:
    filters = ExtractedFilters(similar_to="Catan")
    query, exclude_id = resolve_seed_query(
        seeded_session, filters, "games like Catan"
    )
    seed = seeded_session.get(Game, 1)
    assert exclude_id == 1
    assert query == _document_text(_game_to_row(seed))
    assert query != "games like Catan"


def test_resolve_seed_query_miss_uses_user_query(
    seeded_session: Session,
) -> None:
    filters = ExtractedFilters(similar_to="NoSuchGame")
    query, exclude_id = resolve_seed_query(
        seeded_session, filters, "games like NoSuchGame"
    )
    assert exclude_id is None
    assert query == "games like NoSuchGame"


def test_retrieve_similar_to_drops_seed(
    seeded_session: Session, vector_store: Chroma
) -> None:
    filters = ExtractedFilters(similar_to="Catan")
    results, relaxed = retrieve_games(
        seeded_session, vector_store, filters, "games like Catan", top_k=5
    )
    names = {doc.metadata["name"] for doc in results}
    assert "Catan" not in names
    assert len(results) > 0
    assert relaxed is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_retriever.py::test_resolve_seed_query_hit_uses_document_text tests/test_retriever.py::test_resolve_seed_query_miss_uses_user_query tests/test_retriever.py::test_retrieve_similar_to_drops_seed tests/test_sql_filters.py::test_has_active_hard_filters -v`

Expected: FAIL (`resolve_seed_query` missing and/or Catan still in hits)

- [ ] **Step 3: Minimal implementation**

In `app/retriever.py`, import `lookup_indexed_game_by_name` and `_document_text`, `_game_to_row`.

```python
def resolve_seed_query(
    session: Session,
    filters: ExtractedFilters,
    user_query: str,
) -> tuple[str, int | None]:
    fallback = _search_query(filters, user_query)
    if not filters.similar_to:
        return fallback, None
    seed = lookup_indexed_game_by_name(session, filters.similar_to)
    if seed is None:
        logger.info("similar_to unmatched name=%r", filters.similar_to)
        return fallback, None
    return _document_text(_game_to_row(seed)), seed.id


def _without_game_id(
    documents: list[Document], exclude_id: int | None, top_k: int
) -> list[Document]:
    if exclude_id is None:
        return documents[:top_k]
    kept: list[Document] = []
    for doc in documents:
        raw = doc.metadata.get("game_id")
        try:
            game_id = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            game_id = None
        if game_id == exclude_id:
            continue
        kept.append(doc)
        if len(kept) >= top_k:
            break
    return kept
```

In `retrieve_games`, after `working = apply_category_normalization(...)`:

```python
query, exclude_id = resolve_seed_query(session, working, user_query)
fetch_k = top_k + 1 if exclude_id is not None else top_k
```

Use `fetch_k` in `_similarity_within_ids` and the final `similarity_search`. After each Chroma call:

```python
results = _without_game_id(results, exclude_id, top_k)
if results:
    ...
    return results, filters_relaxed
```

Do not return when `results` is empty after the drop; fall through to `next_relaxation`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_retriever.py tests/test_sql_filters.py::test_has_active_hard_filters -v`

Expected: PASS (existing retriever tests still pass)

- [ ] **Step 5: Commit**

```bash
git add app/retriever.py tests/test_retriever.py tests/test_sql_filters.py
git commit -m "$(cat <<'EOF'
feat: rank neighbors of similar_to seed document

EOF
)"
```

---

### Task 6: LLM prompt + `filters_applied`

**Files:**
- Modify: `app/query_extractor.py`
- Modify: `app/recommender.py` (`filters_to_applied`)
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: `ExtractedFilters.similar_to`
- Produces: prompt instructions + JSON field `similar_to`; `FiltersApplied.similar_to` populated from extract

- [ ] **Step 1: Write the failing API test**

In `tests/test_api.py`, add (same mocks as `test_recommend_response_shape`):

```python
@patch("app.main.synthesize_recommendations")
@patch("app.main.extract_filters")
def test_recommend_filters_applied_includes_similar_to(
    mock_extract: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
) -> None:
    mock_extract.return_value = ExtractedFilters(similar_to="Catan")
    mock_synthesize.return_value = SynthesisOutput(
        recommendations=[
            GameRecommendation(
                name="Azul",
                reason="Similar weight and spatial play.",
                min_players=2,
                max_players=4,
                play_time_minutes=45,
                categories=["abstract"],
            )
        ],
        reasoning="Neighbors of Catan.",
    )
    response = client.post("/recommend", json={"query": "games like Catan"})
    assert response.status_code == 200
    assert response.json()["filters_applied"]["similar_to"] == "Catan"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api.py::test_recommend_filters_applied_includes_similar_to -v`

Expected: FAIL (`similar_to` missing or null in `filters_applied`)

- [ ] **Step 3: Minimal implementation**

In `filters_to_applied`, pass `similar_to=filters.similar_to`.

In `EXTRACTION_PROMPT` system message, after the categories paragraph, add:

```
Similar-to:
- 'games like Catan', 'similar to Ticket to Ride', 'alternatives to Wingspan' → similar_to = the game name only.
- Only set similar_to when a name is explicit. Do not guess.
- Do not put that name in keywords when similar_to is set.
```

In the “Return JSON” field list, add:

```
- similar_to (string game name or null)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_api.py tests/test_llm_parsing.py tests/test_name_match.py tests/test_ingest.py tests/test_retriever.py tests/test_sql_filters.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/query_extractor.py app/recommender.py tests/test_api.py
git commit -m "$(cat <<'EOF'
feat: extract similar_to in the LLM filter prompt

EOF
)"
```

---

## Follow-ups (do not implement in this plan)

- When building catalog `/search`, import `name_match_predicate` / `name_match_order` from `app/name_match.py`.
- When implementing `docs/superpowers/specs/2026-08-20-text-filter-extraction-design.md`, parse `similar_to` from like-phrases so `"like Catan for 4"` still works when the LLM is skipped.
