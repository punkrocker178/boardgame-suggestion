from __future__ import annotations

import math
from copy import deepcopy

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from app.db.models import Category, Game, GameCategory
from app.ingest import _eligible_games_filters
from app.models import ExtractedFilters

# Cap SQL → Chroma allowlist size (ranked games first) for faster vector search.
CANDIDATE_ID_LIMIT = 1000

HARD_FILTER_FIELDS = (
    "player_count",
    "categories",
    "max_play_time_minutes",
    "complexity",
    "min_weight",
    "max_weight",
    "min_age",
    "max_age",
    "min_year",
    "max_year",
    "best_with_player_count",
    "recommended_with_player_count",
)

# Stages 0..7 mutate filters for SQL; None means no further SQL relaxation.
_MAX_SQL_RELAXATION_STAGE = 7


def has_active_hard_filters(filters: ExtractedFilters) -> bool:
    data = filters.model_dump()
    return any(data.get(field) is not None for field in HARD_FILTER_FIELDS)


def _complexity_predicate(complexity: str):
    if complexity == "light":
        return Game.weight < 2.0
    if complexity == "medium":
        return and_(Game.weight >= 2.0, Game.weight < 3.5)
    if complexity == "heavy":
        return Game.weight >= 3.5
    raise ValueError(f"invalid complexity: {complexity}")


def _difficulty_predicates(filters: ExtractedFilters) -> list:
    weight_preds = []
    if filters.min_weight is not None:
        weight_preds.append(Game.weight >= filters.min_weight)
    if filters.max_weight is not None:
        weight_preds.append(Game.weight <= filters.max_weight)

    has_complexity = filters.complexity is not None
    if has_complexity and weight_preds:
        weight_clause = and_(*weight_preds) if len(weight_preds) > 1 else weight_preds[0]
        return [
            or_(
                _complexity_predicate(filters.complexity),
                weight_clause,
            )
        ]
    if has_complexity:
        return [_complexity_predicate(filters.complexity)]
    if weight_preds:
        return [and_(*weight_preds)] if len(weight_preds) > 1 else weight_preds
    return []


def _array_contains(column, value: int):
    """True when INTEGER[] / JSON list column contains value (Postgres + SQLite)."""
    return column.contains([value])


def build_filter_predicates(filters: ExtractedFilters) -> list:
    preds: list = []

    if filters.player_count is not None:
        count = filters.player_count
        preds.append(Game.min_players <= count)
        preds.append(Game.max_players >= count)

    if filters.max_play_time_minutes is not None:
        preds.append(Game.playing_time <= filters.max_play_time_minutes)

    if filters.categories:
        cats = filters.categories
        preds.append(
            Game.id.in_(
                select(GameCategory.game_id)
                .join(Category, Category.id == GameCategory.category_id)
                .where(Category.name.in_(cats))
            )
        )

    preds.extend(_difficulty_predicates(filters))

    if filters.min_age is not None:
        preds.append(Game.min_age >= filters.min_age)
    if filters.max_age is not None:
        preds.append(Game.min_age <= filters.max_age)

    if filters.min_year is not None:
        preds.append(Game.year_published >= filters.min_year)
    if filters.max_year is not None:
        preds.append(Game.year_published <= filters.max_year)

    if filters.best_with_player_count is not None:
        preds.append(_array_contains(Game.best_with_players, filters.best_with_player_count))
    if filters.recommended_with_player_count is not None:
        preds.append(
            _array_contains(
                Game.recommended_with_players, filters.recommended_with_player_count
            )
        )

    return preds


def _candidate_stmt(
    filters: ExtractedFilters, *, limit: int = CANDIDATE_ID_LIMIT
) -> Select[tuple[int]]:
    return (
        select(Game.id)
        .where(*_eligible_games_filters())
        .where(*build_filter_predicates(filters))
        .order_by(Game.rank.asc().nulls_last(), Game.name.asc())
        .limit(limit)
    )


def fetch_candidate_ids(
    session: Session,
    filters: ExtractedFilters,
    *,
    limit: int = CANDIDATE_ID_LIMIT,
) -> list[int]:
    return list(session.scalars(_candidate_stmt(filters, limit=limit)).all())


def _copy_filters(filters: ExtractedFilters) -> ExtractedFilters:
    return ExtractedFilters.model_validate(deepcopy(filters.model_dump()))


def next_relaxation(filters: ExtractedFilters, stage: int) -> ExtractedFilters | None:
    """Apply one relaxation stage. Stages 0..7; None when SQL relaxation is exhausted."""
    if stage < 0 or stage > _MAX_SQL_RELAXATION_STAGE:
        return None

    out = _copy_filters(filters)

    if stage == 0:
        out.min_year = None
        out.max_year = None
    elif stage == 1:
        out.best_with_player_count = None
        out.recommended_with_player_count = None
    elif stage == 2:
        out.categories = None
    elif stage == 3:
        if out.max_play_time_minutes is not None:
            out.max_play_time_minutes = math.ceil(out.max_play_time_minutes * 1.5)
    elif stage == 4:
        out.max_play_time_minutes = None
    elif stage == 5:
        out.complexity = None
        out.min_weight = None
        out.max_weight = None
    elif stage == 6:
        out.min_age = None
        out.max_age = None
    elif stage == 7:
        out.player_count = None

    return out
