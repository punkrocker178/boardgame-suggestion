"""Catalog search: POST /search and GET /search/autocomplete."""
from __future__ import annotations

import base64
import json

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Game, GameCategory
from app.models import (
    AutocompleteGame,
    AutocompleteResponse,
    ExtractedFilters,
    SearchGame,
    SearchRequest,
    SearchResponse,
)
from app.name_match import (
    PG_TRGM_MIN_SIMILARITY,
    _dialect_name,
    name_match_order,
    name_match_predicate,
)
from app.sql_filters import build_filter_predicates


def encode_cursor(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode()


def decode_cursor(token: str) -> dict:
    try:
        decoded = base64.urlsafe_b64decode(token.encode() + b"==")
        result = json.loads(decoded)
        if not isinstance(result, dict):
            raise ValueError("cursor must be a JSON object")
        return result
    except Exception as exc:
        raise ValueError(f"Invalid cursor: {exc}") from exc


def _filters_from_request(req: SearchRequest) -> ExtractedFilters:
    return ExtractedFilters(
        player_count=req.player_count,
        categories=req.categories,
        max_play_time_minutes=req.max_play_time_minutes,
        complexity=req.complexity,
        min_weight=req.min_weight,
        max_weight=req.max_weight,
        min_age=req.min_age,
        max_age=req.max_age,
        min_year=req.min_year,
        max_year=req.max_year,
        best_with_player_count=req.best_with_player_count,
        recommended_with_player_count=req.recommended_with_player_count,
    )


def _browse_after(last_rank, last_id):
    if last_rank is not None:
        return or_(
            Game.rank > last_rank,
            and_(Game.rank == last_rank, Game.id > last_id),
            Game.rank.is_(None),
        )
    return and_(Game.rank.is_(None), Game.id > last_id)


def _sim_value_for_cursor(session: Session, q: str, game: Game) -> float:
    if _dialect_name(session) != "postgresql":
        return 1.0
    val = session.scalar(select(func.similarity(Game.name, q)).where(Game.id == game.id))
    return float(val) if val is not None else 0.0


def search_games(session: Session, request: SearchRequest) -> SearchResponse:
    filters = _filters_from_request(request)
    preds = build_filter_predicates(filters)

    cursor_data: dict | None = None
    if request.cursor:
        cursor_data = decode_cursor(request.cursor)

    q = (request.q or "").strip() or None

    if q:
        preds.append(name_match_predicate(session, q))
        if _dialect_name(session) == "postgresql":
            preds.append(func.similarity(Game.name, q) >= PG_TRGM_MIN_SIMILARITY)
        order = name_match_order(session, q)

        if cursor_data:
            last_sim = cursor_data.get("sim", 1.0)
            last_rank = cursor_data.get("rank")
            last_id = cursor_data["id"]
            if _dialect_name(session) == "postgresql":
                preds.append(
                    or_(
                        func.similarity(Game.name, q) < last_sim,
                        and_(
                            func.similarity(Game.name, q) == last_sim,
                            _browse_after(last_rank, last_id),
                        ),
                    )
                )
            else:
                preds.append(_browse_after(last_rank, last_id))
    else:
        order = (Game.rank.asc().nulls_last(), Game.id.asc())

        if cursor_data:
            last_rank = cursor_data.get("rank")
            last_id = cursor_data["id"]
            preds.append(_browse_after(last_rank, last_id))

    stmt = (
        select(Game)
        .options(selectinload(Game.categories).selectinload(GameCategory.category))
        .where(*preds)
        .order_by(*order)
        .limit(request.limit + 1)
    )

    rows = list(session.scalars(stmt).all())
    has_next = len(rows) > request.limit
    page = rows[: request.limit]

    next_cursor: str | None = None
    if has_next and page:
        last = page[-1]
        if q:
            sim = _sim_value_for_cursor(session, q, last)
            next_cursor = encode_cursor({"sim": sim, "rank": last.rank, "id": last.id})
        else:
            next_cursor = encode_cursor({"rank": last.rank, "id": last.id})

    items = [
        SearchGame(
            id=g.id,
            name=g.name,
            year_published=g.year_published,
            rank=g.rank,
            is_expansion=g.is_expansion,
            min_players=g.min_players,
            max_players=g.max_players,
            playing_time=g.playing_time,
            min_age=g.min_age,
            weight=float(g.weight) if g.weight is not None else None,
            thumbnail_url=g.thumbnail_url,
            categories=[gc.category.name for gc in g.categories],
        )
        for g in page
    ]

    return SearchResponse(items=items, next_cursor=next_cursor)


def autocomplete_games(session: Session, q: str, limit: int) -> AutocompleteResponse:
    # Prefix first (ILIKE 'q%'), then substring fill. Avoids sorting every '%q%'
    # hit; gin_trgm_ops can still serve both patterns (not btree).
    order = (Game.rank.asc().nulls_last(), Game.id.asc())
    prefix_pred = Game.name.ilike(f"{q}%")
    rows = list(
        session.scalars(select(Game).where(prefix_pred).order_by(*order).limit(limit)).all()
    )
    if len(rows) < limit:
        rest = list(
            session.scalars(
                select(Game)
                .where(Game.name.ilike(f"%{q}%"), ~prefix_pred)
                .order_by(*order)
                .limit(limit - len(rows))
            ).all()
        )
        rows = [*rows, *rest]
    return AutocompleteResponse(
        suggestions=[
            AutocompleteGame(id=g.id, name=g.name, year_published=g.year_published)
            for g in rows
        ]
    )
