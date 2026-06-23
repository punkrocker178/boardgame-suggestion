from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.embeddings import FakeEmbeddings

from app.main import app, app_state
from app.models import ExtractedFilters, FiltersApplied, GameRecommendation
from app.recommender import SynthesisOutput


@pytest.fixture
def client(tmp_path, monkeypatch):
    csv_path = tmp_path / "games.csv"
    csv_path.write_text(
        "name,description,min_players,max_players,play_time_minutes,categories,complexity\n"
        "Catan,Trade and build,3,4,90,strategy,medium\n"
    )
    chroma_dir = tmp_path / "chroma"

    monkeypatch.setenv("GAMES_CSV_PATH", str(csv_path))
    monkeypatch.setenv("CHROMA_PERSIST_DIR", str(chroma_dir))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    from app.config import get_settings

    get_settings.cache_clear()

    with patch("app.main.get_embeddings", return_value=FakeEmbeddings(size=8)):
        with TestClient(app) as test_client:
            yield test_client

    get_settings.cache_clear()


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
