import csv
from pathlib import Path

import pytest
from langchain_core.embeddings import FakeEmbeddings

from app.ingest import IngestError, ingest_games, load_games_csv, rows_to_documents


@pytest.fixture
def sample_csv(tmp_path: Path) -> Path:
    path = tmp_path / "games.csv"
    path.write_text(
        "name,description,min_players,max_players,play_time_minutes,categories,complexity\n"
        "Catan,Trade and build,3,4,90,strategy,medium\n"
        "Azul,Draft tiles,2,4,45,abstract,light\n"
    )
    return path


def test_load_games_csv_valid(sample_csv: Path) -> None:
    rows = load_games_csv(sample_csv)
    assert len(rows) == 2
    assert rows[0]["name"] == "Catan"


def test_load_games_csv_missing_file(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="not found"):
        load_games_csv(tmp_path / "missing.csv")


def test_load_games_csv_missing_columns(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("name,description\nCatan,Trade\n")
    with pytest.raises(IngestError, match="missing required columns"):
        load_games_csv(path)


def test_load_games_csv_invalid_player_count(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text(
        "name,description,min_players,max_players,play_time_minutes,categories\n"
        "Bad Game,Desc,4,2,60,strategy\n"
    )
    with pytest.raises(IngestError, match="invalid player count"):
        load_games_csv(path)


def test_load_games_csv_invalid_complexity(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text(
        "name,description,min_players,max_players,play_time_minutes,categories,complexity\n"
        "Bad Game,Desc,2,4,60,strategy,extreme\n"
    )
    with pytest.raises(IngestError, match="invalid complexity"):
        load_games_csv(path)


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


def test_ingest_indexes_correct_count(sample_csv: Path, tmp_path: Path) -> None:
    chroma_dir = tmp_path / "chroma"
    result = ingest_games(sample_csv, chroma_dir, FakeEmbeddings(size=8))
    assert result.indexed_count == 2
    assert result.skipped is False


def test_ingest_skips_unchanged_file(sample_csv: Path, tmp_path: Path) -> None:
    chroma_dir = tmp_path / "chroma"
    embeddings = FakeEmbeddings(size=8)
    first = ingest_games(sample_csv, chroma_dir, embeddings)
    second = ingest_games(sample_csv, chroma_dir, embeddings)
    assert first.skipped is False
    assert second.skipped is True
    assert second.indexed_count == 2
