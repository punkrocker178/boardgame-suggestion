import logging

from langchain_chroma import Chroma
from langchain_core.documents import Document

from app.models import ExtractedFilters

logger = logging.getLogger(__name__)


def _weight_conditions(filters: ExtractedFilters) -> list[dict]:
    conditions: list[dict] = []
    if filters.min_weight is not None:
        conditions.append({"weight": {"$gte": filters.min_weight}})
    if filters.max_weight is not None:
        conditions.append({"weight": {"$lte": filters.max_weight}})
    return conditions


def _difficulty_condition(filters: ExtractedFilters) -> dict | None:
    weight_conds = _weight_conditions(filters)
    has_complexity = filters.complexity is not None
    if has_complexity and weight_conds:
        weight_clause: dict
        if len(weight_conds) == 1:
            weight_clause = weight_conds[0]
        else:
            weight_clause = {"$and": weight_conds}
        return {
            "$or": [
                {"complexity": filters.complexity},
                weight_clause,
            ]
        }
    if has_complexity:
        return {"complexity": filters.complexity}
    if not weight_conds:
        return None
    if len(weight_conds) == 1:
        return weight_conds[0]
    return {"$and": weight_conds}


def build_where_clause(filters: ExtractedFilters) -> dict | None:
    conditions: list[dict] = []

    if filters.player_count is not None:
        count = filters.player_count
        conditions.append({"min_players": {"$lte": count}})
        conditions.append({"max_players": {"$gte": count}})

    if filters.max_play_time_minutes is not None:
        conditions.append(
            {"play_time_minutes": {"$lte": filters.max_play_time_minutes}}
        )

    if filters.categories:
        category_conditions = [
            {"categories": {"$contains": category}} for category in filters.categories
        ]
        if len(category_conditions) == 1:
            conditions.append(category_conditions[0])
        else:
            conditions.append({"$or": category_conditions})

    difficulty = _difficulty_condition(filters)
    if difficulty is not None:
        conditions.append(difficulty)

    if filters.min_age is not None:
        conditions.append({"min_age": {"$gte": filters.min_age}})
    if filters.max_age is not None:
        conditions.append({"min_age": {"$lte": filters.max_age}})

    if filters.min_year is not None:
        conditions.append({"year_published": {"$gte": filters.min_year}})
    if filters.max_year is not None:
        conditions.append({"year_published": {"$lte": filters.max_year}})

    if filters.best_with_player_count is not None:
        token = f"#{filters.best_with_player_count}#"
        conditions.append({"best_with_players": {"$contains": token}})

    if filters.recommended_with_player_count is not None:
        token = f"#{filters.recommended_with_player_count}#"
        conditions.append({"recommended_with_players": {"$contains": token}})

    if not conditions:
        return None
    if len(conditions) == 1:
        clause = conditions[0]
    else:
        clause = {"$and": conditions}
    logger.debug("Built Chroma where clause: %s", clause)
    return clause


def _search_query(filters: ExtractedFilters, user_query: str) -> str:
    parts = [user_query]
    if filters.keywords:
        parts.extend(filters.keywords)
    return " ".join(parts)


def retrieve_games(
    vector_store: Chroma,
    filters: ExtractedFilters,
    user_query: str,
    *,
    top_k: int = 5,
) -> tuple[list[Document], bool]:
    query = _search_query(filters, user_query)
    where = build_where_clause(filters)
    logger.info("Retrieving games query=%r top_k=%d filters=%s", query, top_k, filters.model_dump())

    if where is not None:
        results = vector_store.similarity_search(query, k=top_k, filter=where)
        if results:
            logger.info(
                "Retrieved %d games with metadata filters: %s",
                len(results),
                [doc.metadata.get("name") for doc in results],
            )
            return results, False
        logger.info("Metadata filters matched 0 games; falling back to semantic search")

    results = vector_store.similarity_search(query, k=top_k)
    filters_relaxed = where is not None and len(results) > 0
    logger.info(
        "Retrieved %d games (filters_relaxed=%s): %s",
        len(results),
        filters_relaxed,
        [doc.metadata.get("name") for doc in results],
    )
    return results, filters_relaxed
