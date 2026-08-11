from pathlib import Path

import pytest
from langchain_chroma import Chroma
from langchain_core.embeddings import FakeEmbeddings

from app.ingest import COLLECTION_NAME, rows_to_documents
from app.models import ExtractedFilters
from app.retriever import build_where_clause, retrieve_games


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


def test_build_where_player_count() -> None:
    clause = build_where_clause(ExtractedFilters(player_count=4))
    assert clause == {
        "$and": [
            {"min_players": {"$lte": 4}},
            {"max_players": {"$gte": 4}},
        ]
    }


def test_build_where_categories_or() -> None:
    clause = build_where_clause(ExtractedFilters(categories=["strategy", "party"]))
    assert clause == {
        "$or": [
            {"categories": {"$contains": "strategy"}},
            {"categories": {"$contains": "party"}},
        ]
    }


def test_build_where_complexity_only() -> None:
    clause = build_where_clause(ExtractedFilters(complexity="light"))
    assert clause == {"complexity": "light"}


def test_build_where_weight_only() -> None:
    clause = build_where_clause(ExtractedFilters(min_weight=3.0, max_weight=4.0))
    assert clause == {
        "$and": [
            {"weight": {"$gte": 3.0}},
            {"weight": {"$lte": 4.0}},
        ]
    }


def test_build_where_complexity_or_weight() -> None:
    clause = build_where_clause(
        ExtractedFilters(complexity="light", min_weight=3.0)
    )
    assert clause == {
        "$or": [
            {"complexity": "light"},
            {"weight": {"$gte": 3.0}},
        ]
    }


def test_build_where_best_with_uses_hash_token() -> None:
    clause = build_where_clause(ExtractedFilters(best_with_player_count=4))
    assert clause == {"best_with_players": {"$contains": "#4#"}}


def test_build_where_player_count_does_not_add_poll() -> None:
    clause = build_where_clause(ExtractedFilters(player_count=4))
    assert "best_with_players" not in str(clause)
    assert "recommended_with_players" not in str(clause)


def test_build_where_age_and_year() -> None:
    clause = build_where_clause(
        ExtractedFilters(min_age=12, max_age=10, min_year=2015, max_year=2020)
    )
    assert clause == {
        "$and": [
            {"min_age": {"$gte": 12}},
            {"min_age": {"$lte": 10}},
            {"year_published": {"$gte": 2015}},
            {"year_published": {"$lte": 2020}},
        ]
    }


def test_retrieve_with_player_filter(vector_store: Chroma) -> None:
    filters = ExtractedFilters(player_count=4)
    results, relaxed = retrieve_games(vector_store, filters, "strategy game", top_k=5)
    names = {doc.metadata["name"] for doc in results}
    assert "Catan" in names
    assert "Codenames" in names
    assert relaxed is False


def test_retrieve_fallback_when_no_matches(vector_store: Chroma) -> None:
    filters = ExtractedFilters(player_count=99)
    results, relaxed = retrieve_games(vector_store, filters, "any game", top_k=5)
    assert len(results) > 0
    assert relaxed is True


def test_retrieve_semantic_only_without_filters(vector_store: Chroma) -> None:
    filters = ExtractedFilters()
    results, relaxed = retrieve_games(vector_store, filters, "party word game", top_k=3)
    assert len(results) > 0
    assert relaxed is False
