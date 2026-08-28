from datetime import UTC, datetime
import signal
import threading
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.embeddings import FakeEmbeddings
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Category, CrawlStatus, Game, GameCategory
from app.main import app, app_state
from app.models import ExtractedFilters, GameRecommendation
from app.recommender import SynthesisOutput


@pytest.fixture
def client(tmp_path, monkeypatch):
    chroma_dir = tmp_path / "chroma"
    monkeypatch.setenv("CHROMA_PERSIST_DIR", str(chroma_dir))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        session.add(Category(id=1000, name="Strategy"))
        session.flush()
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
        game.categories.append(GameCategory(category_id=1000))
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


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["indexed_games"] == 1


def test_recommend_empty_query_returns_422(client: TestClient) -> None:
    response = client.post("/recommend", json={"query": ""})
    assert response.status_code == 422


@patch("app.main.synthesize_recommendations")
@patch("app.main.extract_filters")
def test_recommend_response_shape(
    mock_extract: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
) -> None:
    mock_extract.return_value = ExtractedFilters(
        player_count=4,
        categories=["strategy"],
        max_play_time_minutes=60,
        complexity="light",
    )
    mock_synthesize.return_value = SynthesisOutput(
        recommendations=[
            GameRecommendation(
                name="Catan",
                reason="Supports 3-4 players with strategy gameplay.",
                min_players=3,
                max_players=4,
                play_time_minutes=90,
                categories=["strategy"],
            )
        ],
        reasoning="Filtered games down to one strong match.",
    )

    response = client.post(
        "/recommend",
        json={"query": "light strategy game for 4 players under 60 minutes"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "recommendations" in data
    assert "reasoning" in data
    assert "filters_applied" in data
    assert "filters_relaxed" in data
    assert data["filters_applied"]["player_count"] == 4
    assert data["recommendations"][0]["name"] == "Catan"


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


@patch("app.main.synthesize_recommendations")
@patch("app.main.extract_filters")
def test_recommend_no_games_indexed_returns_503(
    mock_extract: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
) -> None:
    app_state.indexing_ok = False
    app_state.indexed_games = 0

    response = client.post("/recommend", json={"query": "any game"})
    assert response.status_code == 503
    assert response.json()["error"] == "No games indexed"

    app_state.indexing_ok = True
    app_state.indexed_games = 1
    app_state.index_stale = False


def test_health_degraded_when_index_stale(client: TestClient) -> None:
    app_state.indexing_ok = True
    app_state.indexed_games = 1
    app_state.index_stale = True
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["indexed_games"] == 1
    app_state.index_stale = False


def test_run_indexing_marks_stale(tmp_path, monkeypatch) -> None:
    from contextlib import contextmanager

    from app.ingest import IngestResult
    from app.main import _run_indexing

    monkeypatch.setenv("CHROMA_PERSIST_DIR", str(tmp_path / "chroma"))
    app_state.settings = app_state.settings.__class__()
    app_state.indexed_games = 0
    app_state.indexing_ok = False
    app_state.index_stale = False

    @contextmanager
    def fake_session_factory():
        yield object()

    with patch(
        "app.main.ingest_games",
        return_value=IngestResult(indexed_count=3, skipped=False, stale=True),
    ), patch("app.main.get_embeddings", return_value=FakeEmbeddings(size=8)), patch(
        "app.main.get_session_factory", return_value=fake_session_factory
    ):
        _run_indexing(app_state.settings)

    assert app_state.indexing_ok is True
    assert app_state.index_stale is True
    assert app_state.indexed_games == 3


def test_chain_cancel_on_signals_sets_event_and_calls_previous() -> None:
    from app.main import _chain_cancel_on_signals

    cancel = threading.Event()
    previous_calls: list[int] = []

    def previous(signum: int, frame: object) -> None:
        previous_calls.append(signum)

    old = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, previous)
    restore = _chain_cancel_on_signals(cancel)
    try:
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
        assert cancel.is_set()
        assert previous_calls == [signal.SIGINT]
    finally:
        restore()
        signal.signal(signal.SIGINT, old)
