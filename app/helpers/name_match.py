from sqlalchemy import func, select, true
from sqlalchemy.orm import Session, selectinload

from app.db.models import Game, GameCategory, GameMechanic
from app.services.ingest import _eligible_games_filters

PG_TRGM_MIN_SIMILARITY = 0.3


def _dialect_name(session: Session) -> str:
    return session.get_bind().dialect.name


def name_match_predicate(session: Session, q: str):
    if _dialect_name(session) == "postgresql":
        return Game.name.op("%")(q)
    return Game.name.ilike(f"%{q}%")


def name_match_order(session: Session, q: str):
    if _dialect_name(session) == "postgresql":
        return (
            func.similarity(Game.name, q).desc(),
            Game.rank.asc().nulls_last(),
            Game.id.asc(),
        )
    return (Game.rank.asc().nulls_last(), Game.id.asc())


def lookup_indexed_game_by_name(session: Session, q: str) -> Game | None:
    needle = (q or "").strip()
    if not needle:
        return None

    filters = [*_eligible_games_filters(), name_match_predicate(session, needle)]
    if _dialect_name(session) == "postgresql":
        filters.append(func.similarity(Game.name, needle) >= PG_TRGM_MIN_SIMILARITY)
    else:
        filters.append(true())

    stmt = (
        select(Game)
        .options(
            selectinload(Game.categories).selectinload(GameCategory.category),
            selectinload(Game.mechanics).selectinload(GameMechanic.mechanic),
        )
        .where(*filters)
        .order_by(*name_match_order(session, needle))
        .limit(1)
    )
    return session.scalars(stmt).first()
