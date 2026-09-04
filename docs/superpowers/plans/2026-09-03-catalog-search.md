# Catalog Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `POST /search` (filter-browse + fuzzy name) and `GET /search/autocomplete` endpoints backed purely by Postgres/SQLite — no LLM, no Chroma.

**Architecture:** A new `app/search.py` holds all query logic (`search_games`, `autocomplete_games`, cursor encode/decode). New Pydantic models go in `app/models.py`. Two thin routes are registered in `app/main.py`. Dialect-aware name matching reuses `app/name_match.py`.

**Tech Stack:** FastAPI, SQLAlchemy (existing), Pydantic v2, `app/name_match.py` (existing), `app/sql_filters.py` (existing).

## Global Constraints

- Python 3.12, SQLAlchemy 2, Pydantic v2 — match existing codebase patterns.
- No new dependencies.
- SQLite used in tests (no pg_trgm); ILIKE used as stand-in (already handled by `name_match_predicate`).
- `pg_trgm` extension + GIN index already exist (`scripts/migrate_pg_trgm.sql`, `scripts/schema.sql`). Do not re-add them.
- Follow naming conventions in `app/models.py` and `tests/test_api.py`.
- `POST /search` must not require an active Chroma index — return 200 even when indexing is down.
- No `total` count in any response.

---

### Task 1: Pydantic models for search

**Files:**
- Modify: `app/models.py`

**Interfaces:**
- Produces:
  - `SearchRequest` — request body for `POST /search`
  - `SearchGame` — one item in `SearchResponse.items`
  - `SearchResponse` — response for `POST /search`
  - `AutocompleteGame` — one suggestion in `AutocompleteResponse`
  - `AutocompleteResponse` — response for `GET /search/autocomplete`

- [ ] **Step 1: Write the failing test**

Create `tests/test_search.py`:

```python
from app.models import (
    AutocompleteGame,
    AutocompleteResponse,
    SearchGame,
    SearchRequest,
    SearchResponse,
)


def test_search_request_defaults():
    req = SearchRequest()
    assert req.limit == 20
    assert req.q is None
    assert req.cursor is None


def test_search_request_limit_clamped():
    req = SearchRequest(limit=100)
    assert req.limit == 50


def test_search_request_limit_min():
    import pytest
    with pytest.raises(Exception):
        SearchRequest(limit=0)


def test_search_response_shape():
    game = SearchGame(
        id=1,
        name="Catan",
        year_published=1995,
        rank=400,
        is_expansion=False,
        min_players=3,
        max_players=4,
        playing_time=90,
        min_age=10,
        weight=2.3,
        thumbnail_url="http://example.com/img.jpg",
        categories=["Strategy"],
    )
    resp = SearchResponse(items=[game], next_cursor=None)
    assert resp.next_cursor is None
    assert resp.items[0].name == "Catan"


def test_autocomplete_response_shape():
    resp = AutocompleteResponse(
        suggestions=[AutocompleteGame(id=1, name="Catan", year_published=1995)]
    )
    assert len(resp.suggestions) == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/hieu/projects/boardgame-suggestion
pytest tests/test_search.py::test_search_request_defaults -v
```

Expected: `ImportError` — models not defined yet.

- [ ] **Step 3: Add models to `app/models.py`**

Append after the existing `HealthResponse` class:

```python
class SearchRequest(BaseModel):
    q: str | None = None
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = None
    # Hard filter fields (same names as ExtractedFilters / HARD_FILTER_FIELDS)
    player_count: int | None = None
    categories: list[str] | None = None
    max_play_time_minutes: int | None = None
    complexity: Literal["light", "medium", "heavy"] | None = None
    min_weight: float | None = None
    max_weight: float | None = None
    min_age: int | None = None
    max_age: int | None = None
    min_year: int | None = None
    max_year: int | None = None
    best_with_player_count: int | None = None
    recommended_with_player_count: int | None = None


class SearchGame(BaseModel):
    id: int
    name: str
    year_published: int | None
    rank: int | None
    is_expansion: bool
    min_players: int | None
    max_players: int | None
    playing_time: int | None
    min_age: int | None
    weight: float | None
    thumbnail_url: str | None
    categories: list[str]


class SearchResponse(BaseModel):
    items: list[SearchGame]
    next_cursor: str | None


class AutocompleteGame(BaseModel):
    id: int
    name: str
    year_published: int | None


class AutocompleteResponse(BaseModel):
    suggestions: list[AutocompleteGame]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_search.py -v -k "model or defaults or shape or clamped or min"
```

Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models.py tests/test_search.py
git commit -m "feat: add search Pydantic models"
```

---

### Task 2: Cursor encode/decode

**Files:**
- Create: `app/search.py`

**Interfaces:**
- Produces:
  - `encode_cursor(data: dict) -> str` — URL-safe base64 JSON
  - `decode_cursor(token: str) -> dict` — raises `ValueError` on bad input

- [ ] **Step 1: Write the failing test**

Add to `tests/test_search.py`:

```python
def test_cursor_roundtrip():
    from app.search import decode_cursor, encode_cursor
    data = {"rank": 5, "id": 42}
    assert decode_cursor(encode_cursor(data)) == data


def test_cursor_invalid_raises():
    import pytest
    from app.search import decode_cursor
    with pytest.raises(ValueError):
        decode_cursor("not-valid-base64!!!")


def test_cursor_tampered_raises():
    import pytest
    import base64
    from app.search import decode_cursor
    bad = base64.urlsafe_b64encode(b"not json").decode()
    with pytest.raises(ValueError):
        decode_cursor(bad)
```

- [ ] **Step 2: Run to verify they fail**

```bash
pytest tests/test_search.py::test_cursor_roundtrip -v
```

Expected: `ModuleNotFoundError` — `app/search.py` not created yet.

- [ ] **Step 3: Create `app/search.py` with cursor functions**

```python
"""Catalog search: POST /search and GET /search/autocomplete."""
from __future__ import annotations

import base64
import json


def encode_cursor(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode()


def decode_cursor(token: str) -> dict:
    try:
        decoded = base64.urlsafe_b64decode(token.encode() + b"==")
        result = json.loads(decoded)
        if not isinstance(result, dict):
            raise ValueError("cursor must be a JSON object")
        return result
    except Exception as exc:
        raise ValueError(f"Invalid cursor: {exc}") from exc
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_search.py::test_cursor_roundtrip tests/test_search.py::test_cursor_invalid_raises tests/test_search.py::test_cursor_tampered_raises -v
```

Expected: all 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/search.py tests/test_search.py
git commit -m "feat: add cursor encode/decode"
```

---

### Task 3: `search_games` — browse and name-match query

**Files:**
- Modify: `app/search.py`

**Interfaces:**
- Consumes:
  - `encode_cursor(data: dict) -> str` (Task 2)
  - `decode_cursor(token: str) -> dict` — raises `ValueError` (Task 2)
  - `name_match_predicate(session, q)` from `app/name_match.py`
  - `name_match_order(session, q)` from `app/name_match.py`
  - `build_filter_predicates(filters: ExtractedFilters)` from `app/sql_filters.py`
  - `SearchRequest`, `SearchGame`, `SearchResponse` from `app/models.py`
  - `Game`, `GameCategory`, `Category` from `app/db/models.py`
- Produces:
  - `search_games(session: Session, request: SearchRequest) -> SearchResponse`

**Cursor design:**

Browse (no `q`): cursor payload `{"rank": int | None, "id": int}`. Pagination predicate:
- If `last_rank` is not None: `(rank > last_rank) OR (rank = last_rank AND id > last_id) OR rank IS NULL`
- If `last_rank` is None: `rank IS NULL AND id > last_id`

With `q`: cursor payload `{"sim": float, "rank": int | None, "id": int}`. Pagination predicate:
- `sim < last_sim OR (sim = last_sim AND <browse predicate for (last_rank, last_id)>)`
- On SQLite, `sim` is always `1.0` (ILIKE hits — no real similarity score), so pagination degrades to rank/id ordering (still correct).

Similarity value for cursor: on Postgres, fetch `func.similarity(Game.name, q)` alongside each row. On SQLite, use constant `1.0`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_search.py`:

```python
import pytest
from datetime import UTC, datetime
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Category, CrawlStatus, Game, GameCategory


@pytest.fixture
def search_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        cat = Category(id=1, name="Strategy")
        session.add(cat)
        session.flush()
        games = [
            Game(id=1, name="Catan", rank=1, is_expansion=False, crawl_status=CrawlStatus.COMPLETED,
                 min_players=3, max_players=4, playing_time=90, weight=2.3,
                 crawled_at=datetime.now(UTC), year_published=1995),
            Game(id=2, name="Azul", rank=2, is_expansion=False, crawl_status=CrawlStatus.COMPLETED,
                 min_players=2, max_players=4, playing_time=45, weight=1.8,
                 crawled_at=datetime.now(UTC), year_published=2017),
            Game(id=3, name="Pandemic", rank=3, is_expansion=False, crawl_status=CrawlStatus.COMPLETED,
                 min_players=2, max_players=4, playing_time=60, weight=2.4,
                 crawled_at=datetime.now(UTC), year_published=2008),
        ]
        games[0].categories.append(GameCategory(category_id=1))
        for g in games:
            session.add(g)
        session.commit()
    yield factory


def test_search_browse_no_filters(search_session):
    from app.models import SearchRequest
    from app.search import search_games
    with search_session() as session:
        resp = search_games(session, SearchRequest(limit=10))
    assert len(resp.items) == 3
    assert resp.next_cursor is None
    assert resp.items[0].id == 1  # rank ASC


def test_search_browse_pagination(search_session):
    from app.models import SearchRequest
    from app.search import search_games
    with search_session() as session:
        page1 = search_games(session, SearchRequest(limit=2))
    assert len(page1.items) == 2
    assert page1.next_cursor is not None

    with search_session() as session:
        page2 = search_games(session, SearchRequest(limit=2, cursor=page1.next_cursor))
    assert len(page2.items) == 1
    assert page2.next_cursor is None
    # no overlap
    ids1 = {g.id for g in page1.items}
    ids2 = {g.id for g in page2.items}
    assert not ids1 & ids2


def test_search_with_q(search_session):
    from app.models import SearchRequest
    from app.search import search_games
    with search_session() as session:
        resp = search_games(session, SearchRequest(q="Catan"))
    assert any(g.name == "Catan" for g in resp.items)


def test_search_filter_player_count(search_session):
    from app.models import SearchRequest
    from app.search import search_games
    with search_session() as session:
        # min_players=3 for Catan; Azul and Pandemic min_players=2
        resp = search_games(session, SearchRequest(player_count=3))
    ids = {g.id for g in resp.items}
    assert 1 in ids  # Catan supports 3


def test_search_includes_expansions(search_session):
    """Search must not apply _eligible_games_filters (expansions included)."""
    from datetime import UTC, datetime
    from app.models import SearchRequest
    from app.search import search_games
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        session.add(Game(id=10, name="Catan Expansion", rank=None, is_expansion=True,
                         crawl_status=CrawlStatus.COMPLETED, crawled_at=datetime.now(UTC)))
        session.commit()
    with factory() as session:
        resp = search_games(session, SearchRequest(limit=50))
    assert any(g.id == 10 for g in resp.items)


def test_search_bad_cursor_returns_value_error(search_session):
    from app.models import SearchRequest
    from app.search import search_games
    with pytest.raises(ValueError):
        with search_session() as session:
            search_games(session, SearchRequest(cursor="BADINPUT!!!"))
```

- [ ] **Step 2: Run to verify they fail**

```bash
pytest tests/test_search.py::test_search_browse_no_filters -v
```

Expected: `ImportError` — `search_games` not defined yet.

- [ ] **Step 3: Implement `search_games` in `app/search.py`**

Append after the cursor functions:

```python
from __future__ import annotations  # already at top

from sqlalchemy import or_, and_, func, select, true
from sqlalchemy.orm import Session, selectinload

from app.db.models import Game, GameCategory, Category
from app.models import ExtractedFilters, SearchGame, SearchRequest, SearchResponse
from app.name_match import name_match_predicate, name_match_order, _dialect_name
from app.sql_filters import build_filter_predicates


def _filters_from_request(req: SearchRequest) -> ExtractedFilters:
    return ExtractedFilters(
        player_count=req.player_count,
        categories=req.categories,
        max_play_time_minutes=req.max_play_time_minutes,
        complexity=req.complexity,
        min_weight=req.min_weight,
        max_weight=req.max_weight,
        min_age=req.min_age,
        max_age=req.max_age,
        min_year=req.min_year,
        max_year=req.max_year,
        best_with_player_count=req.best_with_player_count,
        recommended_with_player_count=req.recommended_with_player_count,
    )


def _browse_after(last_rank, last_id):
    """SQLAlchemy predicate: rows after (rank, id) in (rank ASC NULLS LAST, id ASC) order."""
    if last_rank is not None:
        return or_(
            Game.rank > last_rank,
            and_(Game.rank == last_rank, Game.id > last_id),
            Game.rank.is_(None),
        )
    return and_(Game.rank.is_(None), Game.id > last_id)


def _sim_column(session: Session, q: str):
    if _dialect_name(session) == "postgresql":
        return func.similarity(Game.name, q)
    return true()  # SQLite: constant true (maps to 1 in ORDER BY context)


def _sim_value_for_cursor(session: Session, q: str, game: Game) -> float:
    """Similarity value to store in cursor. SQLite always 1.0."""
    if _dialect_name(session) != "postgresql":
        return 1.0
    # Re-query similarity for the last item
    val = session.scalar(select(func.similarity(Game.name, q)).where(Game.id == game.id))
    return float(val) if val is not None else 0.0


def search_games(session: Session, request: SearchRequest) -> SearchResponse:
    filters = _filters_from_request(request)
    preds = build_filter_predicates(filters)

    # Cursor decode
    cursor_data: dict | None = None
    if request.cursor:
        cursor_data = decode_cursor(request.cursor)  # raises ValueError on bad input

    q = (request.q or "").strip() or None

    if q:
        name_pred = name_match_predicate(session, q)
        if _dialect_name(session) == "postgresql":
            from app.name_match import PG_TRGM_MIN_SIMILARITY
            preds.append(func.similarity(Game.name, q) >= PG_TRGM_MIN_SIMILARITY)
        preds.append(name_pred)
        order = name_match_order(session, q)

        if cursor_data:
            last_sim = cursor_data.get("sim", 1.0)
            last_rank = cursor_data.get("rank")
            last_id = cursor_data["id"]
            if _dialect_name(session) == "postgresql":
                preds.append(
                    or_(
                        func.similarity(Game.name, q) < last_sim,
                        and_(
                            func.similarity(Game.name, q) == last_sim,
                            _browse_after(last_rank, last_id),
                        ),
                    )
                )
            else:
                # SQLite: all ILIKE hits get sim=1.0, paginate by rank/id only
                preds.append(_browse_after(last_rank, last_id))
    else:
        order = (Game.rank.asc().nulls_last(), Game.id.asc())

        if cursor_data:
            last_rank = cursor_data.get("rank")
            last_id = cursor_data["id"]
            preds.append(_browse_after(last_rank, last_id))

    stmt = (
        select(Game)
        .options(selectinload(Game.categories).selectinload(GameCategory.category))
        .where(*preds)
        .order_by(*order)
        .limit(request.limit + 1)
    )

    rows = list(session.scalars(stmt).all())
    has_next = len(rows) > request.limit
    page = rows[: request.limit]

    next_cursor: str | None = None
    if has_next and page:
        last = page[-1]
        if q:
            sim = _sim_value_for_cursor(session, q, last)
            next_cursor = encode_cursor({"sim": sim, "rank": last.rank, "id": last.id})
        else:
            next_cursor = encode_cursor({"rank": last.rank, "id": last.id})

    items = [
        SearchGame(
            id=g.id,
            name=g.name,
            year_published=g.year_published,
            rank=g.rank,
            is_expansion=g.is_expansion,
            min_players=g.min_players,
            max_players=g.max_players,
            playing_time=g.playing_time,
            min_age=g.min_age,
            weight=float(g.weight) if g.weight is not None else None,
            thumbnail_url=g.thumbnail_url,
            categories=[gc.category.name for gc in g.categories],
        )
        for g in page
    ]

    return SearchResponse(items=items, next_cursor=next_cursor)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_search.py -k "search" -v
```

Expected: all `test_search_*` tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/search.py tests/test_search.py
git commit -m "feat: implement search_games with cursor pagination"
```

---

### Task 4: `autocomplete_games` — prefix-then-substring

**Files:**
- Modify: `app/search.py`

**Interfaces:**
- Consumes: `Game` from `app/db/models.py`, `AutocompleteGame`, `AutocompleteResponse` from `app/models.py`
- Produces: `autocomplete_games(session: Session, q: str, limit: int) -> AutocompleteResponse`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_search.py`:

```python
def test_autocomplete_prefix_before_substring(search_session):
    """'Cat' should return 'Catan' (prefix match) before any substring-only match."""
    from app.search import autocomplete_games
    with search_session() as session:
        resp = autocomplete_games(session, "Cat", 10)
    names = [s.name for s in resp.suggestions]
    assert "Catan" in names
    assert names.index("Catan") == 0


def test_autocomplete_substring_match(search_session):
    from app.search import autocomplete_games
    with search_session() as session:
        resp = autocomplete_games(session, "ata", 10)
    assert any(s.name == "Catan" for s in resp.suggestions)


def test_autocomplete_limit(search_session):
    from app.search import autocomplete_games
    with search_session() as session:
        resp = autocomplete_games(session, "a", 2)
    assert len(resp.suggestions) <= 2
```

- [ ] **Step 2: Run to verify they fail**

```bash
pytest tests/test_search.py::test_autocomplete_prefix_before_substring -v
```

Expected: `ImportError` — `autocomplete_games` not defined.

- [ ] **Step 3: Implement `autocomplete_games` in `app/search.py`**

Append after `search_games`:

```python
from app.models import AutocompleteGame, AutocompleteResponse  # add to imports at top


def autocomplete_games(session: Session, q: str, limit: int) -> AutocompleteResponse:
    prefix_pred = Game.name.ilike(f"{q}%")
    substr_pred = Game.name.ilike(f"%{q}%")

    # Fetch limit rows ordered: prefix matches first, then rank ASC NULLS LAST, id ASC
    # Use CASE to sort prefix matches first
    from sqlalchemy import case
    is_prefix = case((prefix_pred, 0), else_=1)

    stmt = (
        select(Game)
        .where(substr_pred)
        .order_by(is_prefix, Game.rank.asc().nulls_last(), Game.id.asc())
        .limit(limit)
    )
    rows = list(session.scalars(stmt).all())
    return AutocompleteResponse(
        suggestions=[
            AutocompleteGame(id=g.id, name=g.name, year_published=g.year_published)
            for g in rows
        ]
    )
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_search.py -k "autocomplete" -v
```

Expected: all 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/search.py tests/test_search.py
git commit -m "feat: implement autocomplete_games"
```

---

### Task 5: Register routes in `app/main.py`

**Files:**
- Modify: `app/main.py`

**Interfaces:**
- Consumes:
  - `search_games(session, request: SearchRequest) -> SearchResponse`
  - `autocomplete_games(session, q: str, limit: int) -> AutocompleteResponse`
  - `SearchRequest`, `SearchResponse`, `AutocompleteResponse` from `app/models.py`
  - `get_session_factory` (already used in `main.py`)
- Produces: `POST /search` and `GET /search/autocomplete` HTTP endpoints

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_search.py` — these need the full `client` fixture. Copy its setup here (same pattern as `tests/test_api.py`):

```python
from unittest.mock import patch
from langchain_core.embeddings import FakeEmbeddings


@pytest.fixture
def search_client(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db.models import Base, Category, CrawlStatus, Game, GameCategory
    from app.main import app

    monkeypatch.setenv("CHROMA_PERSIST_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        cat = Category(id=1, name="Strategy")
        session.add(cat)
        session.flush()
        g = Game(id=1, name="Catan", rank=1, is_expansion=False,
                 crawl_status=CrawlStatus.COMPLETED, min_players=3, max_players=4,
                 playing_time=90, weight=2.3, crawled_at=datetime.now(UTC),
                 year_published=1995)
        g.categories.append(GameCategory(category_id=1))
        session.add(g)
        session.add(Game(id=2, name="Azul", rank=2, is_expansion=False,
                         crawl_status=CrawlStatus.COMPLETED, min_players=2, max_players=4,
                         playing_time=45, weight=1.8, crawled_at=datetime.now(UTC),
                         year_published=2017))
        session.commit()

    from app.config import get_settings
    from app.db import engine as db_engine
    get_settings.cache_clear()
    db_engine.get_engine.cache_clear()
    db_engine.get_session_factory.cache_clear()

    with patch("app.main.get_session_factory", return_value=factory):
        with patch("app.main.get_embeddings", return_value=FakeEmbeddings(size=8)):
            with TestClient(app) as c:
                yield c

    get_settings.cache_clear()
    db_engine.get_engine.cache_clear()
    db_engine.get_session_factory.cache_clear()


def test_post_search_no_body(search_client):
    resp = search_client.post("/search", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "next_cursor" in data


def test_post_search_returns_items(search_client):
    resp = search_client.post("/search", json={"limit": 10})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert items[0]["name"] == "Catan"


def test_post_search_with_q(search_client):
    resp = search_client.post("/search", json={"q": "Catan"})
    assert resp.status_code == 200
    names = [i["name"] for i in resp.json()["items"]]
    assert "Catan" in names


def test_post_search_bad_cursor(search_client):
    resp = search_client.post("/search", json={"cursor": "BADINPUT!!!"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "Invalid cursor"


def test_post_search_limit_out_of_range(search_client):
    resp = search_client.post("/search", json={"limit": 0})
    assert resp.status_code == 422


def test_get_autocomplete(search_client):
    resp = search_client.get("/search/autocomplete", params={"q": "Cat"})
    assert resp.status_code == 200
    assert any(s["name"] == "Catan" for s in resp.json()["suggestions"])


def test_get_autocomplete_q_too_short(search_client):
    resp = search_client.get("/search/autocomplete", params={"q": "a"})
    assert resp.status_code == 422


def test_get_autocomplete_limit(search_client):
    resp = search_client.get("/search/autocomplete", params={"q": "a", "limit": 25})
    assert resp.status_code == 422


def test_search_works_without_chroma(search_client):
    """POST /search must return 200 regardless of indexing state."""
    from app.main import app_state
    old_ok = app_state.indexing_ok
    old_count = app_state.indexed_games
    app_state.indexing_ok = False
    app_state.indexed_games = 0
    try:
        resp = search_client.post("/search", json={})
        assert resp.status_code == 200
    finally:
        app_state.indexing_ok = old_ok
        app_state.indexed_games = old_count
```

- [ ] **Step 2: Run to verify they fail**

```bash
pytest tests/test_search.py::test_post_search_no_body -v
```

Expected: `404` — routes not registered yet.

- [ ] **Step 3: Add imports and routes to `app/main.py`**

Add to the imports block (after existing imports):

```python
from fastapi import Query
from app.models import AutocompleteResponse, SearchRequest, SearchResponse
from app.search import autocomplete_games, search_games
```

Add routes before `app.exception_handler`:

```python
@app.post("/search", response_model=SearchResponse)
def search(request: SearchRequest) -> SearchResponse:
    logger.info("POST /search q=%r limit=%d", request.q, request.limit)
    session_factory = get_session_factory()
    with session_factory() as session:
        try:
            return search_games(session, request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": "Invalid cursor"}) from exc


@app.get("/search/autocomplete", response_model=AutocompleteResponse)
def autocomplete(
    q: str = Query(min_length=2),
    limit: int = Query(default=10, ge=1, le=20),
) -> AutocompleteResponse:
    logger.info("GET /search/autocomplete q=%r", q)
    session_factory = get_session_factory()
    with session_factory() as session:
        return autocomplete_games(session, q, limit)
```

- [ ] **Step 4: Run all search tests**

```bash
pytest tests/test_search.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Run full test suite to check no regressions**

```bash
pytest -v
```

Expected: all tests PASS (no regressions in `tests/test_api.py` etc.).

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_search.py
git commit -m "feat: register POST /search and GET /search/autocomplete routes"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Covered by |
|---|---|
| `POST /search` with JSON body | Task 5 |
| `GET /search/autocomplete` | Task 5 |
| All `HARD_FILTER_FIELDS` filter predicates | Task 3 (via `build_filter_predicates`) |
| Optional `q`, fuzzy on Postgres / ILIKE on SQLite | Task 3 |
| Cursor pagination (browse + with-q) | Task 3 |
| Empty `q` allowed, browse by rank | Task 3 |
| Autocomplete `q` required, min 2 chars | Task 5 (`Query(min_length=2)`) |
| Autocomplete prefix-first order | Task 4 |
| Expansions and pending crawls included (no `_eligible_games_filters`) | Task 3 (no eligibility filter) |
| No `total` in response | Tasks 1, 3 |
| Invalid cursor → 400 `{"error": "Invalid cursor"}` | Tasks 2, 5 |
| `limit` out of range → 422 | Tasks 1, 5 |
| Zero matches → 200 empty | Task 3 (naturally) |
| Search returns 200 even if Chroma/indexing down | Task 5 test |
| `pg_trgm` schema already exists | Pre-existing (`migrate_pg_trgm.sql`) — no new task needed |
| `app/name_match.py` reused | Task 3 |

**No placeholders found.**

**Type consistency:** `SearchGame`, `SearchResponse`, `AutocompleteGame`, `AutocompleteResponse`, `SearchRequest` defined in Task 1 and used consistently in Tasks 3–5. `search_games` and `autocomplete_games` signatures match across Tasks 3/4 and Task 5.
