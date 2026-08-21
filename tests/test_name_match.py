from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models import Category, CrawlStatus, Game, GameCategory
from app.name_match import lookup_indexed_game_by_name


def _eligible(
    session: Session,
    *,
    game_id: int,
    name: str,
    rank: int,
    is_expansion: bool = False,
    status: str = CrawlStatus.COMPLETED,
) -> Game:
    game = Game(
        id=game_id,
        name=name,
        rank=rank,
        is_expansion=is_expansion,
        crawl_status=status,
        description="x",
        min_players=2,
        max_players=4,
        playing_time=60,
        crawled_at=datetime.now(UTC),
    )
    session.add(game)
    return game


def test_lookup_top_hit_by_rank(db_session: Session) -> None:
    db_session.add(Category(id=1, name="Strategy"))
    db_session.flush()
    junior = _eligible(db_session, game_id=1, name="Catan Junior", rank=1)
    junior.categories.append(GameCategory(category_id=1))
    catan = _eligible(db_session, game_id=2, name="Catan", rank=50)
    catan.categories.append(GameCategory(category_id=1))
    db_session.commit()

    hit = lookup_indexed_game_by_name(db_session, "Catan")
    assert hit is not None
    assert hit.id == 1
    assert hit.name == "Catan Junior"


def test_lookup_miss(db_session: Session) -> None:
    assert lookup_indexed_game_by_name(db_session, "NoSuchGame") is None
    assert lookup_indexed_game_by_name(db_session, "  ") is None


def test_lookup_skips_expansion(db_session: Session) -> None:
    db_session.add(Category(id=1, name="Strategy"))
    db_session.flush()
    exp = _eligible(
        db_session, game_id=9, name="Catan", rank=1, is_expansion=True
    )
    exp.categories.append(GameCategory(category_id=1))
    db_session.commit()
    assert lookup_indexed_game_by_name(db_session, "Catan") is None
