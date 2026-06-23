import logging

from langchain_chroma import Chroma
from langchain_core.documents import Document

from app.models import ExtractedFilters

logger = logging.getLogger(__name__)


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

    if filters.complexity is not None:
        conditions.append({"complexity": filters.complexity})

    if filters.categories:
        category_conditions = [
            {"categories": {"$contains": category}} for category in filters.categories
        ]
        if len(category_conditions) == 1:
            conditions.append(category_conditions[0])
        else:
            conditions.append({"$or": category_conditions})

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
