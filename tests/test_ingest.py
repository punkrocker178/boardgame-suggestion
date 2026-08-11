from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.embeddings import FakeEmbeddings
from sqlalchemy.orm import Session

from app.db.models import Category, CrawlStatus, Game, GameCategory
from app.ingest import (
    IngestError,
    compute_games_watermark,
    count_indexed_games,
    encode_player_list,
    ingest_games,
    load_games_for_rag,
    rows_to_documents,
)


class BoomEmbeddings(FakeEmbeddings):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise TimeoutError("embedding unavailable")


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
        min_age=14,
        year_published=2018,
        best_with_players=[3, 4],
        recommended_with_players=[2, 3, 4],
        crawled_at=datetime.now(UTC),
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add(Category(id=1021, name="Economic"))
    session.flush()
    game.categories.append(GameCategory(category_id=1021))
    session.add(game)
    session.commit()
    return game


def test_encode_player_list() -> None:
    assert encode_player_list([2, 3, 4]) == "#2#3#4#"
    assert encode_player_list(None) is None
    assert encode_player_list([]) is None


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
    assert rows[0]["id"] == "1"
    assert rows[0]["name"] == "Brass: Birmingham"
    assert rows[0]["min_players"] == "2"
    assert rows[0]["max_players"] == "4"
    assert rows[0]["play_time_minutes"] == "120"
    assert rows[0]["categories"] == "economic"
    assert rows[0]["complexity"] == "heavy"
    assert rows[0]["weight"] == "3.86"
    assert rows[0]["min_age"] == "14"
    assert rows[0]["year_published"] == "2018"
    assert rows[0]["best_with_players"] == "#3#4#"
    assert rows[0]["recommended_with_players"] == "#2#3#4#"


def test_load_games_for_rag_skips_invalid_player_range(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_eligible(db_session)
    db_session.add(
        Game(
            id=5,
            name="Broken Players",
            rank=5,
            is_expansion=False,
            crawl_status=CrawlStatus.COMPLETED,
            description="Bad player data",
            min_players=2,
            max_players=0,
            playing_time=60,
        )
    )
    db_session.commit()

    with caplog.at_level("WARNING"):
        rows = load_games_for_rag(db_session)

    assert len(rows) == 1
    assert rows[0]["name"] == "Brass: Birmingham"
    assert any("Broken Players" in message for message in caplog.messages)


def test_load_games_for_rag_empty_raises(db_session: Session) -> None:
    with pytest.raises(IngestError, match="import|crawl|eligible"):
        load_games_for_rag(db_session)


def test_load_games_for_rag_all_invalid_raises(db_session: Session) -> None:
    db_session.add(
        Game(
            id=9,
            name="All Bad",
            rank=1,
            is_expansion=False,
            crawl_status=CrawlStatus.COMPLETED,
            description="x",
            min_players=0,
            max_players=0,
            playing_time=30,
        )
    )
    db_session.commit()
    with pytest.raises(IngestError, match="No valid games"):
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
            "id": "42",
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
        }
    ]
    docs = rows_to_documents(rows)
    assert len(docs) == 1
    assert "Catan" in docs[0].page_content
    assert docs[0].metadata["game_id"] == 42
    assert docs[0].metadata["min_players"] == 3
    assert docs[0].metadata["categories"] == "strategy,economic"
    assert docs[0].metadata["weight"] == 2.3
    assert docs[0].metadata["min_age"] == 10
    assert docs[0].metadata["year_published"] == 1995
    assert docs[0].metadata["best_with_players"] == "#4#"


def test_ingest_indexes_correct_count(db_session: Session, tmp_path: Path) -> None:
    _seed_eligible(db_session)
    chroma_dir = tmp_path / "chroma"
    result = ingest_games(db_session, chroma_dir, FakeEmbeddings(size=8))
    assert result.indexed_count == 1
    assert result.skipped is False
    assert result.stale is False
    assert (chroma_dir / ".games_db_watermark").exists()
    assert count_indexed_games(chroma_dir, FakeEmbeddings(size=8)) == 1


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
    assert second.stale is False
    assert second.indexed_count == 1
    assert count_indexed_games(chroma_dir, embeddings) == 1


def test_ingest_keeps_live_on_embed_failure(
    db_session: Session, tmp_path: Path
) -> None:
    game = _seed_eligible(db_session)
    chroma_dir = tmp_path / "chroma"
    ok = FakeEmbeddings(size=8)
    first = ingest_games(db_session, chroma_dir, ok)
    assert first.stale is False
    watermark_before = (chroma_dir / ".games_db_watermark").read_text()

    game.name = "Brass: Birmingham (Broken Refresh)"
    game.updated_at = datetime(2026, 8, 11, tzinfo=UTC)
    db_session.commit()

    second = ingest_games(
        db_session, chroma_dir, BoomEmbeddings(size=8), max_retries=0
    )
    assert second.stale is True
    assert second.indexed_count == 1
    assert (chroma_dir / ".games_db_watermark").read_text() == watermark_before
    assert count_indexed_games(chroma_dir, ok) == 1
    assert not (tmp_path / "chroma_staging").exists()
