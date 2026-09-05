from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_chroma import Chroma
from langchain_core.embeddings import FakeEmbeddings
from sqlalchemy.orm import Session

from app.db.models import Category, CrawlStatus, Game, GameCategory
from app.services.ingest import COLLECTION_NAME, _document_text, _game_to_row, rows_to_documents
from app.api.models import ExtractedFilters
from app.services.retriever import resolve_seed_query, retrieve_games


def _seed_db(session: Session) -> None:
    session.add_all(
        [
            Category(id=1, name="Strategy"),
            Category(id=2, name="Party"),
            Category(id=3, name="Abstract"),
        ]
    )
    session.flush()

    games = [
        Game(
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
            min_age=10,
            year_published=1995,
            best_with_players=[4],
            recommended_with_players=[3, 4],
            crawled_at=datetime.now(UTC),
        ),
        Game(
            id=2,
            name="Codenames",
            rank=2,
            is_expansion=False,
            crawl_status=CrawlStatus.COMPLETED,
            description="Word party game",
            min_players=4,
            max_players=8,
            playing_time=15,
            weight=1.3,
            year_published=2015,
            best_with_players=[6],
            crawled_at=datetime.now(UTC),
        ),
        Game(
            id=3,
            name="Azul",
            rank=3,
            is_expansion=False,
            crawl_status=CrawlStatus.COMPLETED,
            description="Abstract tile game",
            min_players=2,
            max_players=4,
            playing_time=45,
            weight=1.8,
            min_age=8,
            year_published=2017,
            crawled_at=datetime.now(UTC),
        ),
    ]
    games[0].categories.append(GameCategory(category_id=1))
    games[1].categories.append(GameCategory(category_id=2))
    games[2].categories.append(GameCategory(category_id=3))
    session.add_all(games)
    session.commit()


@pytest.fixture
def vector_store(tmp_path: Path) -> Chroma:
    rows = [
        {
            "id": "1",
            "name": "Catan",
            "description": "Trade and build",
            "min_players": "3",
            "max_players": "4",
            "play_time_minutes": "90",
            "categories": "strategy,economic",
            "complexity": "medium",
            "weight": "2.3",
            "min_age": "10",
            "year_published": "1995",
            "best_with_players": "#4#",
            "recommended_with_players": "#3#4#",
        },
        {
            "id": "2",
            "name": "Codenames",
            "description": "Word party game",
            "min_players": "4",
            "max_players": "8",
            "play_time_minutes": "15",
            "categories": "party,word",
            "complexity": "light",
            "weight": "1.3",
            "best_with_players": "#6#",
        },
        {
            "id": "3",
            "name": "Azul",
            "description": "Abstract tile game",
            "min_players": "2",
            "max_players": "4",
            "play_time_minutes": "45",
            "categories": "abstract,family",
            "complexity": "light",
            "weight": "1.8",
            "min_age": "8",
            "year_published": "2017",
        },
    ]
    documents = rows_to_documents(rows)
    return Chroma.from_documents(
        documents=documents,
        embedding=FakeEmbeddings(size=8),
        collection_name=COLLECTION_NAME,
        persist_directory=str(tmp_path / "chroma"),
        ids=[str(doc.metadata["game_id"]) for doc in documents],
    )


@pytest.fixture
def seeded_session(db_session: Session) -> Session:
    _seed_db(db_session)
    return db_session


def test_retrieve_with_player_filter(
    seeded_session: Session, vector_store: Chroma
) -> None:
    filters = ExtractedFilters(player_count=4)
    results, relaxed = retrieve_games(
        seeded_session, vector_store, filters, "strategy game", top_k=5
    )
    names = {doc.metadata["name"] for doc in results}
    assert "Catan" in names
    assert "Codenames" in names
    assert relaxed is False


def test_retrieve_gradual_relax_when_overconstrained(
    seeded_session: Session, vector_store: Chroma
) -> None:
    # player_count=99 matches nothing until player_count is dropped
    filters = ExtractedFilters(player_count=99, min_year=1990)
    results, relaxed = retrieve_games(
        seeded_session, vector_store, filters, "any game", top_k=5
    )
    assert len(results) > 0
    assert relaxed is True


def test_retrieve_play_time_widen_before_drop(
    seeded_session: Session, vector_store: Chroma
) -> None:
    # Azul is 45 min; 30 widens to 45 then matches
    filters = ExtractedFilters(max_play_time_minutes=30, player_count=2)
    results, relaxed = retrieve_games(
        seeded_session, vector_store, filters, "abstract tile", top_k=5
    )
    names = [doc.metadata["name"] for doc in results]
    assert "Azul" in names
    assert relaxed is True


def test_retrieve_semantic_only_without_filters(
    seeded_session: Session, vector_store: Chroma
) -> None:
    filters = ExtractedFilters()
    results, relaxed = retrieve_games(
        seeded_session, vector_store, filters, "party word game", top_k=3
    )
    assert len(results) > 0
    assert relaxed is False


def test_retrieve_normalizes_category_slug(
    seeded_session: Session, vector_store: Chroma
) -> None:
    filters = ExtractedFilters(categories=["strategy"])
    results, relaxed = retrieve_games(
        seeded_session, vector_store, filters, "trade build", top_k=5
    )
    names = {doc.metadata["name"] for doc in results}
    assert names == {"Catan"}
    assert relaxed is False


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
