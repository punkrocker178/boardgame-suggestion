from datetime import UTC, datetime
from math import ceil

import pytest
from sqlalchemy.orm import Session

from app.db.models import Category, CrawlStatus, Game, GameCategory
from app.api.models import ExtractedFilters
from app.helpers.sql_filters import (
    CANDIDATE_ID_LIMIT,
    fetch_candidate_ids,
    has_active_hard_filters,
    next_relaxation,
)


def _seed_games(session: Session) -> None:
    session.add_all(
        [
            Category(id=1, name="Strategy"),
            Category(id=2, name="Party"),
        ]
    )
    session.flush()

    catan = Game(
        id=1,
        name="Catan",
        rank=1,
        is_expansion=False,
        crawl_status=CrawlStatus.COMPLETED,
        description="Trade",
        min_players=3,
        max_players=4,
        playing_time=90,
        weight=2.3,
        min_age=10,
        year_published=1995,
        best_with_players=[4],
        recommended_with_players=[3, 4],
        crawled_at=datetime.now(UTC),
    )
    catan.categories.append(GameCategory(category_id=1))

    codenames = Game(
        id=2,
        name="Codenames",
        rank=2,
        is_expansion=False,
        crawl_status=CrawlStatus.COMPLETED,
        description="Words",
        min_players=4,
        max_players=8,
        playing_time=15,
        weight=1.3,
        year_published=2015,
        best_with_players=[6],
        crawled_at=datetime.now(UTC),
    )
    codenames.categories.append(GameCategory(category_id=2))

    azul = Game(
        id=3,
        name="Azul",
        rank=3,
        is_expansion=False,
        crawl_status=CrawlStatus.COMPLETED,
        description="Tiles",
        min_players=2,
        max_players=4,
        playing_time=45,
        weight=1.8,
        min_age=8,
        year_published=2017,
        crawled_at=datetime.now(UTC),
    )

    pending = Game(
        id=99,
        name="Pending Game",
        rank=4,
        is_expansion=False,
        crawl_status=CrawlStatus.PENDING,
        min_players=2,
        max_players=4,
        playing_time=30,
    )

    session.add_all([catan, codenames, azul, pending])
    session.commit()


@pytest.fixture
def seeded(db_session: Session) -> Session:
    _seed_games(db_session)
    return db_session


def test_fetch_eligible_only(seeded: Session) -> None:
    ids = fetch_candidate_ids(seeded, ExtractedFilters())
    assert set(ids) == {1, 2, 3}


def test_fetch_player_count(seeded: Session) -> None:
    ids = fetch_candidate_ids(seeded, ExtractedFilters(player_count=4))
    assert set(ids) == {1, 2, 3}
    ids = fetch_candidate_ids(seeded, ExtractedFilters(player_count=8))
    assert ids == [2]


def test_fetch_categories_or(seeded: Session) -> None:
    ids = fetch_candidate_ids(
        seeded, ExtractedFilters(categories=["Strategy", "Party"])
    )
    assert set(ids) == {1, 2}


def test_fetch_complexity_light(seeded: Session) -> None:
    ids = fetch_candidate_ids(seeded, ExtractedFilters(complexity="light"))
    assert set(ids) == {2, 3}


def test_fetch_best_with(seeded: Session) -> None:
    ids = fetch_candidate_ids(seeded, ExtractedFilters(best_with_player_count=4))
    assert ids == [1]


def test_fetch_play_time(seeded: Session) -> None:
    ids = fetch_candidate_ids(seeded, ExtractedFilters(max_play_time_minutes=30))
    assert ids == [2]


def test_next_relaxation_order() -> None:
    filters = ExtractedFilters(
        player_count=4,
        categories=["Strategy"],
        max_play_time_minutes=30,
        complexity="light",
        min_age=8,
        max_age=12,
        min_year=2010,
        max_year=2020,
        best_with_player_count=4,
        recommended_with_player_count=3,
    )
    stage0 = next_relaxation(filters, 0)
    assert stage0 is not None
    assert stage0.min_year is None and stage0.max_year is None
    assert stage0.player_count == 4

    stage1 = next_relaxation(stage0, 1)
    assert stage1 is not None
    assert stage1.best_with_player_count is None
    assert stage1.recommended_with_player_count is None

    stage2 = next_relaxation(stage1, 2)
    assert stage2 is not None
    assert stage2.categories is None

    stage3 = next_relaxation(stage2, 3)
    assert stage3 is not None
    assert stage3.max_play_time_minutes == ceil(30 * 1.5)

    stage4 = next_relaxation(stage3, 4)
    assert stage4 is not None
    assert stage4.max_play_time_minutes is None

    stage5 = next_relaxation(stage4, 5)
    assert stage5 is not None
    assert stage5.complexity is None

    stage6 = next_relaxation(stage5, 6)
    assert stage6 is not None
    assert stage6.min_age is None and stage6.max_age is None

    stage7 = next_relaxation(stage6, 7)
    assert stage7 is not None
    assert stage7.player_count is None

    assert next_relaxation(stage7, 8) is None


def test_has_active_hard_filters() -> None:
    assert has_active_hard_filters(ExtractedFilters()) is False
    assert has_active_hard_filters(ExtractedFilters(player_count=4)) is True
    assert has_active_hard_filters(ExtractedFilters(similar_to="Catan")) is False


def test_fetch_candidate_ids_respects_limit(db_session: Session) -> None:
    db_session.add(Category(id=1, name="Strategy"))
    db_session.flush()
    for i in range(1, CANDIDATE_ID_LIMIT + 50):
        game = Game(
            id=i,
            name=f"Game {i:04d}",
            rank=i,
            is_expansion=False,
            crawl_status=CrawlStatus.COMPLETED,
            description="x",
            min_players=2,
            max_players=4,
            playing_time=30,
            crawled_at=datetime.now(UTC),
        )
        game.categories.append(GameCategory(category_id=1))
        db_session.add(game)
    db_session.commit()

    ids = fetch_candidate_ids(db_session, ExtractedFilters())
    assert len(ids) == CANDIDATE_ID_LIMIT
    assert ids == list(range(1, CANDIDATE_ID_LIMIT + 1))
