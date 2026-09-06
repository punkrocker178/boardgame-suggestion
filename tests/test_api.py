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
from app.api.models import ExtractedFilters, GameRecommendation
from app.main import app
from app.services.recommender import SynthesisOutput
from app.state import app_state


def _conversation_id(client: TestClient) -> str:
    response = client.post("/conversations", json={})
    assert response.status_code == 201
    return response.json()["id"]


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

    with patch("app.main.get_session_factory", return_value=factory), patch(
        "app.api.routes.get_session_factory", return_value=factory
    ), patch("app.main.get_embeddings", return_value=FakeEmbeddings(size=8)), patch(
        "app.api.routes.get_embeddings", return_value=FakeEmbeddings(size=8)
    ):
        with TestClient(app) as test_client:
            yield test_client

    get_settings.cache_clear()
    db_engine.get_engine.cache_clear()
    db_engine.get_session_factory.cache_clear()


def test_create_conversation_returns_id(client: TestClient) -> None:
    response = client.post("/conversations", json={"title": "x"})
    assert response.status_code == 201
    assert "id" in response.json()


def test_recommend_unknown_conversation_returns_404(client: TestClient) -> None:
    response = client.post(
        "/recommend",
        json={
            "conversation_id": "00000000-0000-0000-0000-000000000099",
            "query": "any",
        },
    )
    assert response.status_code == 404


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["indexed_games"] == 1


def test_recommend_empty_query_returns_422(client: TestClient) -> None:
    response = client.post("/recommend", json={"query": ""})
    assert response.status_code == 422


@patch("app.api.routes.synthesize_recommendations")
@patch("app.api.routes.resolve_filters")
def test_recommend_response_shape(
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
) -> None:
    mock_resolve.return_value = ExtractedFilters(
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
        json={
            "conversation_id": _conversation_id(client),
            "query": "light strategy game for 4 players under 60 minutes",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "recommendations" in data
    assert "reasoning" in data
    assert "filters_applied" in data
    assert "filters_relaxed" in data
    assert "conversation_id" in data
    assert "standalone_query" in data
    assert "topic_changed" in data
    assert data["filters_applied"]["player_count"] == 4
    assert data["recommendations"][0]["name"] == "Catan"


@patch("app.api.routes.synthesize_recommendations")
@patch("app.api.routes.resolve_filters")
def test_recommend_filters_applied_includes_similar_to(
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
) -> None:
    mock_resolve.return_value = ExtractedFilters(similar_to="Catan")
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
    response = client.post(
        "/recommend",
        json={
            "conversation_id": _conversation_id(client),
            "query": "games like Catan",
        },
    )
    assert response.status_code == 200
    assert response.json()["filters_applied"]["similar_to"] == "Catan"


@patch("app.api.routes.synthesize_recommendations")
@patch("app.api.routes.resolve_filters")
def test_recommend_no_games_indexed_returns_503(
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
) -> None:
    app_state.indexing_ok = False
    app_state.indexed_games = 0

    response = client.post(
        "/recommend",
        json={
            "conversation_id": _conversation_id(client),
            "query": "any game",
        },
    )
    assert response.status_code == 503
    assert response.json()["error"] == "No games indexed"

    app_state.indexing_ok = True
    app_state.indexed_games = 1
    app_state.index_stale = False


@patch("app.api.routes.synthesize_recommendations")
@patch("app.helpers.query_extractor.extract_filters")
def test_recommend_text_path_does_not_call_llm_extract(
    mock_extract: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
) -> None:
    mock_extract.side_effect = AssertionError("LLM extract should not run")
    mock_synthesize.return_value = SynthesisOutput(
        recommendations=[
            GameRecommendation(
                name="Catan",
                reason="Fits player count.",
                min_players=3,
                max_players=4,
                play_time_minutes=90,
                categories=["strategy"],
            )
        ],
        reasoning="ok",
    )
    response = client.post(
        "/recommend",
        json={
            "conversation_id": _conversation_id(client),
            "query": "for 4 players",
        },
    )
    assert response.status_code == 200
    assert response.json()["filters_applied"]["player_count"] == 4
    mock_extract.assert_not_called()


@patch("app.api.routes.resolve_filters")
def test_recommend_missing_llm_returns_502_after_extraction(
    mock_resolve: MagicMock,
    client: TestClient,
) -> None:
    mock_resolve.return_value = ExtractedFilters(player_count=4)
    previous = app_state.llm
    app_state.llm = None
    try:
        response = client.post(
        "/recommend",
        json={
            "conversation_id": _conversation_id(client),
            "query": "for 4 players",
        },
    )
        assert response.status_code == 502
        assert response.json()["error"] == "LLM unavailable"
        mock_resolve.assert_called_once()
    finally:
        app_state.llm = previous


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

    from app.services.ingest import IngestResult
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


@patch("app.api.routes.synthesize_recommendations")
@patch("app.api.routes.resolve_filters")
@patch("app.services.contextualizer.invoke_structured")
def test_recommend_first_turn_skips_contextualizer_llm(
    mock_invoke: MagicMock,
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
) -> None:
    mock_resolve.return_value = ExtractedFilters(player_count=4)
    mock_synthesize.return_value = SynthesisOutput(
        recommendations=[
            GameRecommendation(
                name="Catan",
                reason="Fits.",
                min_players=3,
                max_players=4,
                play_time_minutes=90,
                categories=["strategy"],
            )
        ],
        reasoning="ok",
    )
    cid = _conversation_id(client)
    response = client.post(
        "/recommend",
        json={"conversation_id": cid, "query": "games for 4 players"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["conversation_id"] == cid
    assert data["standalone_query"] == "games for 4 players"
    assert data["topic_changed"] is False
    mock_invoke.assert_not_called()
    mock_resolve.assert_called_once()
    assert mock_resolve.call_args.args[1] == "games for 4 players"


@patch("app.api.routes.synthesize_recommendations")
@patch("app.api.routes.resolve_filters")
def test_recommend_follow_up_uses_standalone_query(
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
    monkeypatch,
) -> None:
    mock_resolve.return_value = ExtractedFilters(player_count=4)
    mock_synthesize.return_value = SynthesisOutput(
        recommendations=[
            GameRecommendation(
                name="Catan",
                reason="ok",
                min_players=3,
                max_players=4,
                play_time_minutes=90,
                categories=["strategy"],
            )
        ],
        reasoning="ok",
    )
    cid = _conversation_id(client)
    client.post("/recommend", json={"conversation_id": cid, "query": "games for 4 players"})

    from app.services.contextualizer import QueryPlan

    def fake_contextualize(llm, *, query, summary, recent_messages):
        assert recent_messages
        return QueryPlan("light complexity games for 4 players", False)

    monkeypatch.setattr("app.api.routes.contextualize_query", fake_contextualize)
    response = client.post(
        "/recommend",
        json={"conversation_id": cid, "query": "something lighter"},
    )
    assert response.status_code == 200
    assert response.json()["standalone_query"] == "light complexity games for 4 players"
    assert response.json()["topic_changed"] is False
    assert mock_resolve.call_args.args[1] == "light complexity games for 4 players"


@patch("app.api.routes.synthesize_recommendations")
@patch("app.api.routes.resolve_filters")
@patch("app.api.routes.summarize_dropped_turn", return_value="User likes 4-player games.")
def test_recommend_refreshes_summary_when_window_exceeded(
    mock_summarize: MagicMock,
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
    monkeypatch,
) -> None:
    from app.services.contextualizer import QueryPlan

    mock_resolve.return_value = ExtractedFilters()
    mock_synthesize.return_value = SynthesisOutput(
        recommendations=[
            GameRecommendation(
                name="Catan",
                reason="ok",
                min_players=3,
                max_players=4,
                play_time_minutes=90,
                categories=["strategy"],
            )
        ],
        reasoning="ok",
    )
    monkeypatch.setattr(
        "app.api.routes.contextualize_query",
        lambda *a, **k: QueryPlan(k["query"], False),
    )
    cid = _conversation_id(client)
    for i in range(6):
        client.post("/recommend", json={"conversation_id": cid, "query": f"q{i}"})
    assert mock_summarize.called


def _ok_synthesis() -> SynthesisOutput:
    return SynthesisOutput(
        recommendations=[
            GameRecommendation(
                name="Catan",
                reason="ok",
                min_players=3,
                max_players=4,
                play_time_minutes=90,
                categories=["strategy"],
            )
        ],
        reasoning="ok",
    )


@patch("app.api.routes.synthesize_recommendations")
@patch("app.api.routes.resolve_filters")
def test_recommend_cue_keeps_epoch(
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
    monkeypatch,
) -> None:
    from uuid import UUID

    from sqlalchemy import select

    from app.api import routes
    from app.db.models import Conversation, Message
    from app.services.contextualizer import QueryPlan

    mock_resolve.return_value = ExtractedFilters(player_count=4)
    mock_synthesize.return_value = _ok_synthesis()
    cid = _conversation_id(client)
    client.post("/recommend", json={"conversation_id": cid, "query": "party games for 8"})

    def fake_contextualize(llm, *, query, summary, recent_messages):
        assert recent_messages
        return QueryPlan("lighter party games for 8", False)

    monkeypatch.setattr("app.api.routes.contextualize_query", fake_contextualize)
    response = client.post(
        "/recommend",
        json={"conversation_id": cid, "query": "something lighter"},
    )
    assert response.status_code == 200
    assert response.json()["topic_changed"] is False
    factory = routes.get_session_factory()
    with factory() as session:
        conv = session.get(Conversation, UUID(cid))
        assert conv.topic_started_at is None
        users = list(
            session.scalars(
                select(Message).where(
                    Message.conversation_id == UUID(cid), Message.role == "user"
                )
            )
        )
        assert len(users) == 2


@patch("app.api.routes.synthesize_recommendations")
@patch("app.api.routes.resolve_filters")
def test_recommend_topic_switch_uses_raw_query_and_moves_epoch(
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
    monkeypatch,
) -> None:
    from uuid import UUID

    from sqlalchemy import select

    from app.api import routes
    from app.db.models import Conversation, Message
    from app.services.contextualizer import QueryPlan

    mock_resolve.return_value = ExtractedFilters()
    mock_synthesize.return_value = _ok_synthesis()
    cid = _conversation_id(client)
    client.post(
        "/recommend", json={"conversation_id": cid, "query": "party games for 8"}
    )

    def fake_contextualize(llm, *, query, summary, recent_messages):
        return QueryPlan(query, True)

    monkeypatch.setattr("app.api.routes.contextualize_query", fake_contextualize)
    response = client.post(
        "/recommend",
        json={"conversation_id": cid, "query": "2-player war games"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["topic_changed"] is True
    assert data["standalone_query"] == "2-player war games"
    assert mock_resolve.call_args.args[1] == "2-player war games"
    assert mock_synthesize.call_args.args[1] == "2-player war games"
    assert "8" not in mock_resolve.call_args.args[1]
    assert "party" not in mock_resolve.call_args.args[1].lower()

    factory = routes.get_session_factory()
    with factory() as session:
        conv = session.get(Conversation, UUID(cid))
        assert conv.summary is None
        switch_user = session.scalars(
            select(Message)
            .where(Message.conversation_id == UUID(cid), Message.role == "user")
            .order_by(Message.created_at.desc())
        ).first()
        assert conv.topic_started_at == switch_user.created_at

    seen: list[list[str]] = []

    def record_recent(llm, *, query, summary, recent_messages):
        seen.append([m.content for m in recent_messages])
        return QueryPlan(query, False)

    monkeypatch.setattr("app.api.routes.contextualize_query", record_recent)
    client.post(
        "/recommend",
        json={"conversation_id": cid, "query": "something lighter"},
    )
    assert seen
    assert "party games for 8" not in seen[0]


@patch("app.api.routes.synthesize_recommendations")
@patch("app.api.routes.resolve_filters")
def test_recommend_no_cue_without_switch_keeps_epoch(
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
    monkeypatch,
) -> None:
    from uuid import UUID

    from app.api import routes
    from app.db.models import Conversation
    from app.services.contextualizer import QueryPlan

    mock_resolve.return_value = ExtractedFilters()
    mock_synthesize.return_value = _ok_synthesis()
    cid = _conversation_id(client)
    client.post("/recommend", json={"conversation_id": cid, "query": "games for 4"})

    monkeypatch.setattr(
        "app.api.routes.contextualize_query",
        lambda *a, **k: QueryPlan("cooperative games for 4", False),
    )
    response = client.post(
        "/recommend",
        json={"conversation_id": cid, "query": "cooperative games"},
    )
    assert response.json()["standalone_query"] == "cooperative games for 4"
    assert response.json()["topic_changed"] is False
    factory = routes.get_session_factory()
    with factory() as session:
        conv = session.get(Conversation, UUID(cid))
        assert conv.topic_started_at is None


@patch("app.api.routes.synthesize_recommendations")
@patch("app.api.routes.resolve_filters")
@patch("app.api.routes.summarize_dropped_turn", return_value="New topic summary.")
def test_recommend_summary_after_switch_drops_new_epoch_only(
    mock_summarize: MagicMock,
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
    monkeypatch,
) -> None:
    from app.services.contextualizer import QueryPlan

    mock_resolve.return_value = ExtractedFilters()
    mock_synthesize.return_value = _ok_synthesis()
    cid = _conversation_id(client)
    client.post("/recommend", json={"conversation_id": cid, "query": "party games for 8"})

    plans = iter(
        [QueryPlan("2-player war games", True)]
        + [QueryPlan(f"new{i}", False) for i in range(6)]
    )

    def fake_contextualize(llm, *, query, summary, recent_messages):
        return next(plans)

    monkeypatch.setattr("app.api.routes.contextualize_query", fake_contextualize)
    client.post(
        "/recommend", json={"conversation_id": cid, "query": "2-player war games"}
    )
    mock_summarize.reset_mock()
    for i in range(6):
        client.post("/recommend", json={"conversation_id": cid, "query": f"new{i}"})
    assert mock_summarize.called
    dropped_users = [c.kwargs["user_content"] for c in mock_summarize.call_args_list]
    assert "party games for 8" not in dropped_users
    assert "2-player war games" in dropped_users

