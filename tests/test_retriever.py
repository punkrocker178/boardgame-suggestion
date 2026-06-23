from pathlib import Path

import pytest
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import FakeEmbeddings

from app.ingest import COLLECTION_NAME, rows_to_documents
from app.models import ExtractedFilters
from app.retriever import build_where_clause, retrieve_games


@pytest.fixture
def vector_store(tmp_path: Path) -> Chroma:
    rows = [
        {
            "name": "Catan",
            "description": "Trade and build",
            "min_players": "3",
            "max_players": "4",
            "play_time_minutes": "90",
            "categories": "strategy,economic",
            "complexity": "medium",
        },
        {
            "name": "Codenames",
            "description": "Word party game",
            "min_players": "4",
            "max_players": "8",
            "play_time_minutes": "15",
            "categories": "party,word",
            "complexity": "light",
        },
        {
            "name": "Azul",
            "description": "Abstract tile game",
            "min_players": "2",
            "max_players": "4",
            "play_time_minutes": "45",
            "categories": "abstract,family",
            "complexity": "light",
        },
    ]
    documents = rows_to_documents(rows)
    return Chroma.from_documents(
        documents=documents,
        embedding=FakeEmbeddings(size=8),
        collection_name=COLLECTION_NAME,
        persist_directory=str(tmp_path / "chroma"),
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
