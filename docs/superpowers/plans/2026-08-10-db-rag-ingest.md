# DB-backed RAG ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Index Chroma from Postgres at API startup (no CSV), remove export script and `games.csv`.

**Architecture:** `load_games_for_rag(session)` queries eligible games and maps them to the former CSV row shape. `ingest_games` builds Chroma documents and skips re-index when a DB watermark file is unchanged. Lifespan opens a DB session and fails startup on ingest/DB errors. Retrieval path stays on Chroma.

**Tech Stack:** SQLAlchemy ORM (`Game` / `GameCategory`), existing Chroma + LangChain ingest, FastAPI lifespan, pytest + in-memory SQLite fixtures

**Spec:** `docs/superpowers/specs/2026-08-10-db-rag-ingest-design.md`

## Global Constraints

- No CSV fallback at runtime; zero eligible games or DB failure → ingest/startup failure
- Eligibility: `crawl_status == completed`, `is_expansion == false`, `min_players` / `max_players` / `playing_time` all NOT NULL (include `max_players` so row validation always passes)
- Order: `rank` ASC NULLS LAST, then `name` ASC
- Watermark file: `chroma_dir / ".games_db_watermark"` with value `{count}:{max_updated_at_iso_or_empty}`
- Delete `scripts/export_games_csv.py`, `tests/test_export_games_csv.py`, `data/games.csv`
- Remove `GAMES_CSV_PATH` / `games_csv_path` everywhere
- Keep Chroma; do not introduce pgvector
- Prefer existing `tests/conftest.py` `db_session` (SQLite in-memory) for loader/ingest tests

---

## File map

| File | Responsibility |
|------|----------------|
| `app/ingest.py` | DB load, watermark, document build, Chroma ingest (no CSV) |
| `app/main.py` | Lifespan: session → ingest; hard-fail on `IngestError` |
| `app/config.py` | Drop `games_csv_path` |
| `tests/test_ingest.py` | Loader + watermark + ingest tests against `db_session` |
| `tests/test_api.py` | Seed SQLite games; patch session factory; no CSV env |
| `scripts/export_games_csv.py` | Delete |
| `tests/test_export_games_csv.py` | Delete |
| `data/games.csv` | Delete |
| `Dockerfile` | Drop CSV copy |
| `docker-compose.yml` | Drop `GAMES_CSV_PATH` |
| `.env.example` | Drop `GAMES_CSV_PATH` |
| `docs/database.md` | Crawl → API indexes from DB |
| `README.md` | DB prerequisite; drop CSV docs |

---

### Task 1: `load_games_for_rag` + watermark helpers

**Files:**
- Modify: `app/ingest.py`
- Modify: `tests/test_ingest.py` (replace CSV loader tests; keep `rows_to_documents` test)
- Delete after Task 4 (or this task once loader lives in ingest): logic currently in `scripts/export_games_csv.py` — do not delete the script until Task 4 so git history stays clear; stop importing it from tests in this task

**Interfaces:**
- Consumes: `Session`, `Game`, `GameCategory`, `CrawlStatus`, `complexity_from_weight`
- Produces:
  - `load_games_for_rag(session: Session) -> list[dict[str, str]]`
  - `compute_games_watermark(session: Session) -> str`
  - Keep existing: `validate_row`, `rows_to_documents`, `IngestError`, `IngestResult`, `get_vector_store`

- [ ] **Step 1: Rewrite failing tests for DB loader**

Replace CSV-focused tests in `tests/test_ingest.py` with:

```python
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.embeddings import FakeEmbeddings
from sqlalchemy.orm import Session

from app.db.models import CrawlStatus, Game, GameCategory
from app.ingest import (
    IngestError,
    compute_games_watermark,
    load_games_for_rag,
    rows_to_documents,
)


def _seed_eligible(session: Session) -> Game:
    game = Game(
        id=1,
        name="Brass: Birmingham",
        rank=1,
        is_expansion=False,
        crawl_status=CrawlStatus.COMPLETED,
        description="Industrial revolution game.",
        min_players=2,
        max_players=4,
        playing_time=120,
        weight=3.86,
        crawled_at=datetime.now(UTC),
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    game.categories.append(GameCategory(category="Economic"))
    session.add(game)
    session.commit()
    return game


def test_load_games_for_rag_filters_and_formats(db_session: Session) -> None:
    _seed_eligible(db_session)
    db_session.add_all(
        [
            Game(
                id=2,
                name="Missing Data",
                rank=2,
                is_expansion=False,
                crawl_status=CrawlStatus.COMPLETED,
                min_players=None,
                max_players=4,
                playing_time=None,
            ),
            Game(
                id=3,
                name="Pending Game",
                rank=3,
                is_expansion=False,
                crawl_status=CrawlStatus.PENDING,
                min_players=2,
                max_players=4,
                playing_time=60,
            ),
            Game(
                id=4,
                name="Expansion Pack",
                rank=4,
                is_expansion=True,
                crawl_status=CrawlStatus.COMPLETED,
                min_players=2,
                max_players=4,
                playing_time=60,
            ),
        ]
    )
    db_session.commit()

    rows = load_games_for_rag(db_session)
    assert len(rows) == 1
    assert rows[0]["name"] == "Brass: Birmingham"
    assert rows[0]["min_players"] == "2"
    assert rows[0]["max_players"] == "4"
    assert rows[0]["play_time_minutes"] == "120"
    assert rows[0]["categories"] == "economic"
    assert rows[0]["complexity"] == "heavy"


def test_load_games_for_rag_empty_raises(db_session: Session) -> None:
    with pytest.raises(IngestError, match="import|crawl|eligible"):
        load_games_for_rag(db_session)


def test_compute_games_watermark_stable(db_session: Session) -> None:
    _seed_eligible(db_session)
    first = compute_games_watermark(db_session)
    second = compute_games_watermark(db_session)
    assert first == second
    assert first.startswith("1:")


def test_rows_to_documents_metadata() -> None:
    rows = [
        {
            "name": "Catan",
            "description": "Trade and build",
            "min_players": "3",
            "max_players": "4",
            "play_time_minutes": "90",
            "categories": "strategy,economic",
            "complexity": "medium",
        }
    ]
    docs = rows_to_documents(rows)
    assert len(docs) == 1
    assert "Catan" in docs[0].page_content
    assert docs[0].metadata["min_players"] == 3
    assert docs[0].metadata["categories"] == "strategy,economic"
```

Leave ingest index/skip tests for Task 2 (or delete old CSV ingest tests now so the file collects only loader tests).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest.py::test_load_games_for_rag_filters_and_formats tests/test_ingest.py::test_load_games_for_rag_empty_raises tests/test_ingest.py::test_compute_games_watermark_stable -v`

Expected: FAIL (import errors / missing symbols)

- [ ] **Step 3: Implement loader + watermark in `app/ingest.py`**

Remove CSV imports (`csv`, `_hash_file` for file bytes). Keep `validate_row` / `rows_to_documents`. Add:

```python
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.bgg.parser import complexity_from_weight
from app.db.models import CrawlStatus, Game

WATERMARK_FILENAME = ".games_db_watermark"
# remove HASH_FILENAME / CSV-only helpers later in Task 2 if still present


def _eligible_games_stmt():
    return (
        select(Game)
        .options(selectinload(Game.categories))
        .where(
            Game.crawl_status == CrawlStatus.COMPLETED,
            Game.is_expansion.is_(False),
            Game.min_players.is_not(None),
            Game.max_players.is_not(None),
            Game.playing_time.is_not(None),
        )
        .order_by(Game.rank.asc().nulls_last(), Game.name.asc())
    )


def _game_to_row(game: Game) -> dict[str, str]:
    description = game.description or game.name
    categories = ",".join(
        category.category.lower().replace(" ", "_") for category in game.categories
    )
    complexity = complexity_from_weight(
        float(game.weight) if game.weight is not None else None
    )
    row = {
        "name": game.name,
        "description": description,
        "min_players": str(game.min_players),
        "max_players": str(game.max_players),
        "play_time_minutes": str(game.playing_time),
        "categories": categories or "strategy",
    }
    if complexity:
        row["complexity"] = complexity
    return row


def load_games_for_rag(session: Session) -> list[dict[str, str]]:
    games = session.scalars(_eligible_games_stmt()).all()
    if not games:
        raise IngestError(
            "No eligible games in database; run import + crawl before starting the API"
        )
    rows: list[dict[str, str]] = []
    for index, game in enumerate(games, start=1):
        row = _game_to_row(game)
        validate_row(row, index)
        rows.append(row)
    logger.debug("Loaded %d eligible games from database", len(rows))
    return rows


def compute_games_watermark(session: Session) -> str:
    count = session.scalar(
        select(func.count()).select_from(Game).where(
            Game.crawl_status == CrawlStatus.COMPLETED,
            Game.is_expansion.is_(False),
            Game.min_players.is_not(None),
            Game.max_players.is_not(None),
            Game.playing_time.is_not(None),
        )
    )
    max_updated = session.scalar(
        select(func.max(Game.updated_at)).where(
            Game.crawl_status == CrawlStatus.COMPLETED,
            Game.is_expansion.is_(False),
            Game.min_players.is_not(None),
            Game.max_players.is_not(None),
            Game.playing_time.is_not(None),
        )
    )
    if isinstance(max_updated, datetime):
        stamp = max_updated.isoformat()
    else:
        stamp = ""
    return f"{int(count or 0)}:{stamp}"
```

Note: SQLite in tests may not support `nulls_last()` the same way as Postgres. If ordering tests fail on SQLite, use `.order_by(Game.rank.asc(), Game.name.asc())` for both, or wrap with a dialect-safe order. Prefer keeping `nulls_last()` for Postgres production; if SQLite errors, switch to `nullslast()` only when dialect is PostgreSQL, else plain `asc()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ingest.py -v`

Expected: PASS for loader/watermark/`rows_to_documents` tests. Remove or skip obsolete CSV tests so the file is green.

- [ ] **Step 5: Commit**

```bash
git add app/ingest.py tests/test_ingest.py
git commit -m "$(cat <<'EOF'
Add DB loader and watermark helpers for RAG ingest.

EOF
)"
```

---

### Task 2: DB-backed `ingest_games`

**Files:**
- Modify: `app/ingest.py` (`ingest_games` signature and body)
- Modify: `tests/test_ingest.py` (index + skip tests)

**Interfaces:**
- Consumes: `load_games_for_rag`, `compute_games_watermark`, `rows_to_documents`
- Produces: `ingest_games(session: Session, chroma_dir: Path, embeddings: Embeddings, *, force: bool = False) -> IngestResult`

- [ ] **Step 1: Add failing ingest tests**

```python
def test_ingest_indexes_correct_count(db_session: Session, tmp_path: Path) -> None:
    _seed_eligible(db_session)
    chroma_dir = tmp_path / "chroma"
    result = ingest_games(db_session, chroma_dir, FakeEmbeddings(size=8))
    assert result.indexed_count == 1
    assert result.skipped is False
    assert (chroma_dir / ".games_db_watermark").exists()


def test_ingest_skips_unchanged_watermark(db_session: Session, tmp_path: Path) -> None:
    _seed_eligible(db_session)
    chroma_dir = tmp_path / "chroma"
    embeddings = FakeEmbeddings(size=8)
    first = ingest_games(db_session, chroma_dir, embeddings)
    second = ingest_games(db_session, chroma_dir, embeddings)
    assert first.skipped is False
    assert second.skipped is True
    assert second.indexed_count == 1


def test_ingest_reindexes_when_watermark_changes(
    db_session: Session, tmp_path: Path
) -> None:
    game = _seed_eligible(db_session)
    chroma_dir = tmp_path / "chroma"
    embeddings = FakeEmbeddings(size=8)
    first = ingest_games(db_session, chroma_dir, embeddings)
    game.name = "Brass: Birmingham (Revised)"
    game.updated_at = datetime(2026, 8, 10, tzinfo=UTC)
    db_session.commit()
    second = ingest_games(db_session, chroma_dir, embeddings)
    assert first.skipped is False
    assert second.skipped is False
    assert second.indexed_count == 1
```

Import `ingest_games` in the test module.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest.py::test_ingest_indexes_correct_count tests/test_ingest.py::test_ingest_skips_unchanged_watermark -v`

Expected: FAIL (signature still CSV-based)

- [ ] **Step 3: Implement DB `ingest_games`**

Replace CSV-based `ingest_games` with:

```python
def _watermark_path(chroma_dir: Path) -> Path:
    return chroma_dir / WATERMARK_FILENAME


def ingest_games(
    session: Session,
    chroma_dir: Path,
    embeddings: Embeddings,
    *,
    force: bool = False,
) -> IngestResult:
    chroma_dir.mkdir(parents=True, exist_ok=True)
    current_watermark = compute_games_watermark(session)
    watermark_file = _watermark_path(chroma_dir)

    if (
        not force
        and watermark_file.exists()
        and watermark_file.read_text().strip() == current_watermark
    ):
        rows = load_games_for_rag(session)
        logger.info(
            "Skipping re-index; DB watermark unchanged (%d games)", len(rows)
        )
        return IngestResult(indexed_count=len(rows), skipped=True)

    logger.info("Indexing games from database into %s", chroma_dir)
    rows = load_games_for_rag(session)
    documents = rows_to_documents(rows)

    Chroma.from_documents(
        documents=documents,
        embedding=embeddings,
        collection_name=COLLECTION_NAME,
        persist_directory=str(chroma_dir),
    )

    watermark_file.write_text(current_watermark)
    logger.info(
        "Indexed %d games into Chroma collection %s",
        len(documents),
        COLLECTION_NAME,
    )
    return IngestResult(indexed_count=len(documents), skipped=False)
```

Delete unused CSV helpers: `load_games_csv`, `_hash_file`, `_hash_path`, `HASH_FILENAME`, and `import csv` if still present.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_ingest.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/ingest.py tests/test_ingest.py
git commit -m "$(cat <<'EOF'
Index Chroma from Postgres using a DB watermark.

EOF
)"
```

---

### Task 3: Wire FastAPI lifespan to DB ingest

**Files:**
- Modify: `app/main.py`
- Modify: `app/config.py` (remove `games_csv_path` — can land here or Task 4; do it here so main no longer references it)

**Interfaces:**
- Consumes: `get_session_factory`, `ingest_games(session, chroma_dir, embeddings)`
- Produces: lifespan that hard-fails on `IngestError` (re-raise after log so process does not stay up degraded from missing data)

- [ ] **Step 1: Update `_run_indexing` and lifespan**

In `app/main.py`:

```python
from app.db.engine import get_session_factory
from app.ingest import IngestError, get_vector_store, ingest_games


def _run_indexing(settings: Settings) -> None:
    chroma_dir = Path(settings.chroma_persist_dir)
    embeddings = get_embeddings(settings)
    session_factory = get_session_factory()
    with session_factory() as session:
        result = ingest_games(session, chroma_dir, embeddings)
    app_state.indexed_games = result.indexed_count
    app_state.indexing_ok = True
    if result.skipped:
        logger.info(
            "Skipped re-index; DB watermark unchanged (%d games)",
            result.indexed_count,
        )
    else:
        logger.info("Indexed %d games into Chroma", result.indexed_count)


@asynccontextmanager
async def lifespan(_: FastAPI):
    app_state.settings = get_settings()
    configure_logging(app_state.settings.log_level)
    logger.info("Starting Board Game RAG Game Master")
    app_state.llm = get_llm(app_state.settings)
    try:
        _run_indexing(app_state.settings)
    except IngestError:
        logger.exception("Failed to index games from database")
        app_state.indexing_ok = False
        app_state.indexed_games = 0
        raise
    yield
```

Also catch unexpected DB errors the same way if desired — at minimum let them propagate (do not set degraded and continue). Spec: startup fails.

Remove `games_csv_path` from `Settings` in `app/config.py`.

- [ ] **Step 2: Smoke-check imports**

Run: `python -c "from app.main import app; from app.config import Settings; assert not hasattr(Settings(), 'games_csv_path') or 'games_csv_path' not in Settings.model_fields"`

Simpler check:

```bash
python -c "from app.config import Settings; assert 'games_csv_path' not in Settings.model_fields"
python -c "from app.main import _run_indexing"
```

Expected: no AttributeError / ImportError

- [ ] **Step 3: Commit**

```bash
git add app/main.py app/config.py
git commit -m "$(cat <<'EOF'
Wire API startup ingest to Postgres and drop CSV settings.

EOF
)"
```

---

### Task 4: Remove CSV export path and update docs/Docker

**Files:**
- Delete: `scripts/export_games_csv.py`
- Delete: `tests/test_export_games_csv.py`
- Delete: `data/games.csv`
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `docs/database.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: none (cleanup)
- Produces: stack with no CSV ingest surface

- [ ] **Step 1: Delete obsolete files**

```bash
rm scripts/export_games_csv.py tests/test_export_games_csv.py data/games.csv
```

- [ ] **Step 2: Update Dockerfile**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 3: Update `docker-compose.yml` api environment**

Remove `GAMES_CSV_PATH` line; keep:

```yaml
    environment:
      DATABASE_URL: postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}
      CHROMA_PERSIST_DIR: ./data/chroma
```

- [ ] **Step 4: Update `.env.example`**

Remove the `GAMES_CSV_PATH=./data/games.csv` line.

- [ ] **Step 5: Update `docs/database.md`**

Replace section `## Optional: crawl and export for RAG` with:

```markdown
## Crawl metadata for RAG

After the dump is loaded:

```bash
# Fetch full metadata from the BGG API (needs BGG_API_TOKEN in .env)
python scripts/crawl_bgg_metadata.py
```

The API indexes eligible completed non-expansion games from Postgres into Chroma on startup. There is no CSV export step. Startup fails if the database is unreachable or has no eligible games.
```

- [ ] **Step 6: Update `README.md`**

- Config table: remove `GAMES_CSV_PATH`; add `DATABASE_URL` (host example already in `.env.example`)
- Replace **Data** section with:

```markdown
## Data

1. Start Postgres (`docker compose up -d db`).
2. Import a BGG ranks dump (`python scripts/import_bgg_dump.py --csv ...`).
3. Crawl metadata (`python scripts/crawl_bgg_metadata.py`).
4. Start the API — it re-indexes Chroma when the DB watermark (eligible count + max `updated_at`) changes.

See [docs/database.md](docs/database.md).
```

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Remove CSV RAG export path and document DB-backed ingest.

EOF
)"
```

---

### Task 5: Fix API tests for DB-backed startup

**Files:**
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: `get_session_factory` patched to in-memory SQLite with one eligible game; FakeEmbeddings
- Produces: green API tests without `GAMES_CSV_PATH`

- [ ] **Step 1: Rewrite `client` fixture**

```python
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.embeddings import FakeEmbeddings
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, CrawlStatus, Game, GameCategory
from app.main import app, app_state
from app.models import ExtractedFilters, GameRecommendation
from app.recommender import SynthesisOutput


@pytest.fixture
def client(tmp_path, monkeypatch):
    chroma_dir = tmp_path / "chroma"
    monkeypatch.setenv("CHROMA_PERSIST_DIR", str(chroma_dir))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        game = Game(
            id=1,
            name="Catan",
            rank=1,
            is_expansion=False,
            crawl_status=CrawlStatus.COMPLETED,
            description="Trade and build",
            min_players=3,
            max_players=4,
            playing_time=90,
            weight=2.3,
            crawled_at=datetime.now(UTC),
        )
        game.categories.append(GameCategory(category="Strategy"))
        session.add(game)
        session.commit()

    from app.config import get_settings
    from app.db import engine as db_engine

    get_settings.cache_clear()
    db_engine.get_engine.cache_clear()
    db_engine.get_session_factory.cache_clear()

    with patch("app.main.get_session_factory", return_value=factory):
        with patch("app.main.get_embeddings", return_value=FakeEmbeddings(size=8)):
            with TestClient(app) as test_client:
                yield test_client

    get_settings.cache_clear()
    db_engine.get_engine.cache_clear()
    db_engine.get_session_factory.cache_clear()
```

Keep existing `test_health_ok`, `test_recommend_*` bodies unchanged aside from imports if needed.

- [ ] **Step 2: Run full test suite**

Run: `pytest -v`

Expected: PASS (all tests)

- [ ] **Step 3: Commit**

```bash
git add tests/test_api.py
git commit -m "$(cat <<'EOF'
Point API tests at an in-memory DB for startup ingest.

EOF
)"
```

---

## Plan self-review

| Spec requirement | Task |
|------------------|------|
| DB load with export eligibility/mapping | Task 1 (`max_players` NOT NULL added for validation safety) |
| Watermark skip/re-index | Task 2 |
| Lifespan uses DB; no CSV | Task 3 |
| Hard fail on empty/unreachable | Task 3 (`raise` after log) |
| Delete export script + CSV + config/docs/Docker | Task 4 |
| Tests use DB fixtures | Tasks 1, 2, 5 |
| Out of scope (pgvector, crawl Compose service) | Not planned |

No TBD placeholders. Signatures: `load_games_for_rag(session)`, `compute_games_watermark(session)`, `ingest_games(session, chroma_dir, embeddings, *, force=False)` consistent across tasks.
