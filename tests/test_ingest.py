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
