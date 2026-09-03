import pytest
from datetime import UTC, datetime
from fastapi.testclient import TestClient
from langchain_core.embeddings import FakeEmbeddings
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch

from app.db.models import Base, Category, CrawlStatus, Game, GameCategory
from app.models import (
    AutocompleteGame,
    AutocompleteResponse,
    SearchGame,
    SearchRequest,
    SearchResponse,
)


def test_search_request_defaults():
    req = SearchRequest()
    assert req.limit == 20
    assert req.q is None
    assert req.cursor is None


def test_search_request_limit_max_rejected():
    with pytest.raises(Exception):
        SearchRequest(limit=100)


def test_search_request_limit_min():
    with pytest.raises(Exception):
        SearchRequest(limit=0)


def test_search_response_shape():
    game = SearchGame(
        id=1,
        name="Catan",
        year_published=1995,
        rank=400,
        is_expansion=False,
        min_players=3,
        max_players=4,
        playing_time=90,
        min_age=10,
        weight=2.3,
        thumbnail_url="http://example.com/img.jpg",
        categories=["Strategy"],
    )
    resp = SearchResponse(items=[game], next_cursor=None)
    assert resp.next_cursor is None
    assert resp.items[0].name == "Catan"


def test_autocomplete_response_shape():
    resp = AutocompleteResponse(
        suggestions=[AutocompleteGame(id=1, name="Catan", year_published=1995)]
    )
    assert len(resp.suggestions) == 1


def test_cursor_roundtrip():
    from app.search import decode_cursor, encode_cursor

    data = {"rank": 5, "id": 42}
    assert decode_cursor(encode_cursor(data)) == data


def test_cursor_invalid_raises():
    from app.search import decode_cursor

    with pytest.raises(ValueError):
        decode_cursor("not-valid-base64!!!")


def test_cursor_tampered_raises():
    import base64

    from app.search import decode_cursor

    bad = base64.urlsafe_b64encode(b"not json").decode()
    with pytest.raises(ValueError):
        decode_cursor(bad)


@pytest.fixture
def search_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        cat = Category(id=1, name="Strategy")
        session.add(cat)
        session.flush()
        games = [
            Game(
                id=1,
                name="Catan",
                rank=1,
                is_expansion=False,
                crawl_status=CrawlStatus.COMPLETED,
                min_players=3,
                max_players=4,
                playing_time=90,
                weight=2.3,
                crawled_at=datetime.now(UTC),
                year_published=1995,
            ),
            Game(
                id=2,
                name="Azul",
                rank=2,
                is_expansion=False,
                crawl_status=CrawlStatus.COMPLETED,
                min_players=2,
                max_players=4,
                playing_time=45,
                weight=1.8,
                crawled_at=datetime.now(UTC),
                year_published=2017,
            ),
            Game(
                id=3,
                name="Pandemic",
                rank=3,
                is_expansion=False,
                crawl_status=CrawlStatus.COMPLETED,
                min_players=2,
                max_players=4,
                playing_time=60,
                weight=2.4,
                crawled_at=datetime.now(UTC),
                year_published=2008,
            ),
        ]
        games[0].categories.append(GameCategory(category_id=1))
        for g in games:
            session.add(g)
        session.commit()
    yield factory


def test_search_browse_no_filters(search_session):
    from app.search import search_games

    with search_session() as session:
        resp = search_games(session, SearchRequest(limit=10))
    assert len(resp.items) == 3
    assert resp.next_cursor is None
    assert resp.items[0].id == 1


def test_search_browse_pagination(search_session):
    from app.search import search_games

    with search_session() as session:
        page1 = search_games(session, SearchRequest(limit=2))
    assert len(page1.items) == 2
    assert page1.next_cursor is not None

    with search_session() as session:
        page2 = search_games(session, SearchRequest(limit=2, cursor=page1.next_cursor))
    assert len(page2.items) == 1
    assert page2.next_cursor is None
    ids1 = {g.id for g in page1.items}
    ids2 = {g.id for g in page2.items}
    assert not ids1 & ids2


def test_search_with_q(search_session):
    from app.search import search_games

    with search_session() as session:
        resp = search_games(session, SearchRequest(q="Catan"))
    assert any(g.name == "Catan" for g in resp.items)


def test_search_filter_player_count(search_session):
    from app.search import search_games

    with search_session() as session:
        resp = search_games(session, SearchRequest(player_count=3))
    ids = {g.id for g in resp.items}
    assert 1 in ids


def test_search_includes_expansions(search_session):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        session.add(
            Game(
                id=10,
                name="Catan Expansion",
                rank=None,
                is_expansion=True,
                crawl_status=CrawlStatus.COMPLETED,
                crawled_at=datetime.now(UTC),
            )
        )
        session.commit()
    from app.search import search_games

    with factory() as session:
        resp = search_games(session, SearchRequest(limit=50))
    assert any(g.id == 10 for g in resp.items)


def test_search_bad_cursor_returns_value_error(search_session):
    from app.search import search_games

    with pytest.raises(ValueError):
        with search_session() as session:
            search_games(session, SearchRequest(cursor="BADINPUT!!!"))


def test_autocomplete_prefix_before_substring(search_session):
    from app.search import autocomplete_games

    with search_session() as session:
        resp = autocomplete_games(session, "Cat", 10)
    names = [s.name for s in resp.suggestions]
    assert "Catan" in names
    assert names.index("Catan") == 0


def test_autocomplete_substring_match(search_session):
    from app.search import autocomplete_games

    with search_session() as session:
        resp = autocomplete_games(session, "ata", 10)
    assert any(s.name == "Catan" for s in resp.suggestions)


def test_autocomplete_limit(search_session):
    from app.search import autocomplete_games

    with search_session() as session:
        resp = autocomplete_games(session, "a", 2)
    assert len(resp.suggestions) <= 2


@pytest.fixture
def search_client(tmp_path, monkeypatch):
    from app.main import app

    monkeypatch.setenv("CHROMA_PERSIST_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        cat = Category(id=1, name="Strategy")
        session.add(cat)
        session.flush()
        g = Game(
            id=1,
            name="Catan",
            rank=1,
            is_expansion=False,
            crawl_status=CrawlStatus.COMPLETED,
            min_players=3,
            max_players=4,
            playing_time=90,
            weight=2.3,
            crawled_at=datetime.now(UTC),
            year_published=1995,
        )
        g.categories.append(GameCategory(category_id=1))
        session.add(g)
        session.add(
            Game(
                id=2,
                name="Azul",
                rank=2,
                is_expansion=False,
                crawl_status=CrawlStatus.COMPLETED,
                min_players=2,
                max_players=4,
                playing_time=45,
                weight=1.8,
                crawled_at=datetime.now(UTC),
                year_published=2017,
            )
        )
        session.commit()

    from app.config import get_settings
    from app.db import engine as db_engine

    get_settings.cache_clear()
    db_engine.get_engine.cache_clear()
    db_engine.get_session_factory.cache_clear()

    with patch("app.main.get_session_factory", return_value=factory):
        with patch("app.main.get_embeddings", return_value=FakeEmbeddings(size=8)):
            with TestClient(app) as c:
                yield c

    get_settings.cache_clear()
    db_engine.get_engine.cache_clear()
    db_engine.get_session_factory.cache_clear()


def test_post_search_no_body(search_client):
    resp = search_client.post("/search", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "next_cursor" in data


def test_post_search_returns_items(search_client):
    resp = search_client.post("/search", json={"limit": 10})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert items[0]["name"] == "Catan"


def test_post_search_with_q(search_client):
    resp = search_client.post("/search", json={"q": "Catan"})
    assert resp.status_code == 200
    names = [i["name"] for i in resp.json()["items"]]
    assert "Catan" in names


def test_post_search_bad_cursor(search_client):
    resp = search_client.post("/search", json={"cursor": "BADINPUT!!!"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "Invalid cursor"


def test_post_search_limit_out_of_range(search_client):
    resp = search_client.post("/search", json={"limit": 0})
    assert resp.status_code == 422


def test_get_autocomplete(search_client):
    resp = search_client.get("/search/autocomplete", params={"q": "Cat"})
    assert resp.status_code == 200
    assert any(s["name"] == "Catan" for s in resp.json()["suggestions"])


def test_get_autocomplete_q_too_short(search_client):
    resp = search_client.get("/search/autocomplete", params={"q": "a"})
    assert resp.status_code == 422


def test_get_autocomplete_limit(search_client):
    resp = search_client.get("/search/autocomplete", params={"q": "aa", "limit": 25})
    assert resp.status_code == 422


def test_search_works_without_chroma(search_client):
    from app.main import app_state

    old_ok = app_state.indexing_ok
    old_count = app_state.indexed_games
    app_state.indexing_ok = False
    app_state.indexed_games = 0
    try:
        resp = search_client.post("/search", json={})
        assert resp.status_code == 200
    finally:
        app_state.indexing_ok = old_ok
        app_state.indexed_games = old_count
