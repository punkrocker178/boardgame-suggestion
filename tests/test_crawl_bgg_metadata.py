from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy.orm import Session

from app.db.models import (
    Category,
    CrawlStatus,
    Game,
    GameCategory,
    GameMechanic,
    Mechanic,
)
from scripts.crawl_bgg_metadata import _fetch_batch, crawl
from tests.fixtures.bgg_thing import SAMPLE_THING_XML


def test_game_taxonomy_fk_and_arrays(db_session: Session) -> None:
    db_session.add(Category(id=1021, name="Economic"))
    db_session.add(Mechanic(id=2081, name="Network Building"))
    game = Game(id=1, name="Test", is_expansion=False)
    game.best_with_players = [4, 5]
    game.recommended_with_players = [3, 4, 5, 6]
    game.categories.append(GameCategory(category_id=1021))
    game.mechanics.append(GameMechanic(mechanic_id=2081))
    db_session.add(game)
    db_session.commit()

    db_session.refresh(game)
    assert game.categories[0].category.name == "Economic"
    assert game.mechanics[0].mechanic.name == "Network Building"
    assert game.best_with_players == [4, 5]
    assert game.recommended_with_players == [3, 4, 5, 6]


def test_fetch_batch_retries_on_503() -> None:
    responses = [
        httpx.Response(503, request=httpx.Request("GET", "https://example.com")),
        httpx.Response(
            200,
            text=SAMPLE_THING_XML,
            request=httpx.Request("GET", "https://example.com"),
        ),
    ]
    client = MagicMock()
    client.get.side_effect = responses

    with patch("scripts.crawl_bgg_metadata.time.sleep"):
        result = _fetch_batch(client, [224517], max_retries=5)

    assert "224517" in result
    assert client.get.call_count == 2


def test_fetch_batch_auth_failure() -> None:
    client = MagicMock()
    client.get.return_value = httpx.Response(
        401, request=httpx.Request("GET", "https://example.com")
    )

    with pytest.raises(RuntimeError, match="authentication failed"):
        _fetch_batch(client, [224517], max_retries=1)


def test_crawl_marks_games_completed(db_session: Session) -> None:
    game = Game(
        id=224517,
        name="Brass: Birmingham",
        rank=1,
        is_expansion=False,
        crawl_status=CrawlStatus.PENDING,
    )
    db_session.add(game)
    db_session.commit()

    mock_response = httpx.Response(
        200,
        text=SAMPLE_THING_XML,
        request=httpx.Request("GET", "https://boardgamegeek.com/xmlapi2/thing"),
    )

    with (
        patch("scripts.crawl_bgg_metadata.get_settings") as mock_settings,
        patch("scripts.crawl_bgg_metadata.httpx.Client") as mock_client_cls,
        patch("scripts.crawl_bgg_metadata.time.sleep"),
    ):
        mock_settings.return_value.bgg_api_token = "test-token"
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        stats = crawl(
            db_session,
            batch_size=20,
            delay=0,
            max_batches=1,
            max_retries=3,
            max_attempts=3,
            include_unranked=False,
            include_expansions=False,
            reset_failed=False,
        )

    db_session.refresh(game)
    assert stats.batches == 1
    assert stats.completed == 1
    assert game.crawl_status == CrawlStatus.COMPLETED
    assert game.min_players == 2
    assert game.max_players == 4
    assert game.playing_time == 120
    assert len(game.categories) == 2
