import logging

from langchain_chroma import Chroma
from langchain_core.documents import Document
from sqlalchemy.orm import Session

from app.category_normalize import apply_category_normalization
from app.models import ExtractedFilters
from app.sql_filters import (
    CANDIDATE_ID_LIMIT,
    fetch_candidate_ids,
    has_active_hard_filters,
    next_relaxation,
)

logger = logging.getLogger(__name__)

CHROMA_IN_LIMIT = CANDIDATE_ID_LIMIT


def _search_query(filters: ExtractedFilters, user_query: str) -> str:
    parts = [user_query]
    if filters.keywords:
        parts.extend(filters.keywords)
    return " ".join(parts)


def _post_filter_by_ids(
    documents: list[Document], id_set: set[int], top_k: int
) -> list[Document]:
    matched: list[Document] = []
    for doc in documents:
        raw = doc.metadata.get("game_id")
        if raw is None:
            continue
        try:
            game_id = int(raw)
        except (TypeError, ValueError):
            continue
        if game_id in id_set:
            matched.append(doc)
        if len(matched) >= top_k:
            break
    return matched


def _similarity_within_ids(
    vector_store: Chroma,
    query: str,
    ids: list[int],
    *,
    top_k: int,
) -> list[Document]:
    if not ids:
        return []

    if len(ids) <= CHROMA_IN_LIMIT:
        results = vector_store.similarity_search(
            query, k=top_k, filter={"game_id": {"$in": ids}}
        )
        if results:
            return results
        logger.debug("Chroma $in returned 0; retrying with post-filter")

    over_fetch = min(200, max(top_k * 20, 50))
    raw = vector_store.similarity_search(query, k=over_fetch)
    return _post_filter_by_ids(raw, set(ids), top_k)


def retrieve_games(
    session: Session,
    vector_store: Chroma,
    filters: ExtractedFilters,
    user_query: str,
    *,
    top_k: int = 5,
) -> tuple[list[Document], bool]:
    working = apply_category_normalization(session, filters)
    query = _search_query(working, user_query)
    logger.info(
        "Retrieving games query=%r top_k=%d filters=%s",
        query,
        top_k,
        working.model_dump(),
    )

    filters_relaxed = False
    current = working
    stage = 0

    while True:
        ids = fetch_candidate_ids(session, current)
        logger.info(
            "SQL candidates=%d next_stage=%d filters=%s",
            len(ids),
            stage,
            current.model_dump(),
        )

        if ids:
            results = _similarity_within_ids(
                vector_store, query, ids, top_k=top_k
            )
            if results:
                logger.info(
                    "Retrieved %d games (filters_relaxed=%s): %s",
                    len(results),
                    filters_relaxed,
                    [doc.metadata.get("name") for doc in results],
                )
                return results, filters_relaxed
            logger.info(
                "Allowlist had %d ids but Chroma returned 0; relaxing further",
                len(ids),
            )

        nxt = next_relaxation(current, stage)
        if nxt is None:
            break

        if nxt.model_dump() != current.model_dump():
            filters_relaxed = True
        current = nxt
        stage += 1

    results = vector_store.similarity_search(query, k=top_k)
    had_constraints = has_active_hard_filters(working)
    filters_relaxed = filters_relaxed or (had_constraints and len(results) > 0)
    logger.info(
        "Retrieved %d games via unfiltered semantic (filters_relaxed=%s): %s",
        len(results),
        filters_relaxed,
        [doc.metadata.get("name") for doc in results],
    )
    return results, filters_relaxed
