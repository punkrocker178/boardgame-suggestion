import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from openai import APIConnectionError, APIStatusError
from pydantic import ValidationError

from app.api.models import (
    AutocompleteResponse,
    ConversationCreateRequest,
    ConversationCreateResponse,
    HealthResponse,
    RecommendRequest,
    RecommendResponse,
    SearchRequest,
    SearchResponse,
)
from app.config import get_embeddings
from app.db.engine import get_session_factory
from app.helpers.query_extractor import resolve_filters
from app.services.contextualizer import contextualize_query, summarize_dropped_turn
from app.services.conversation_store import (
    RECENT_TURN_LIMIT,
    append_turn,
    count_turns,
    create_conversation,
    get_conversation,
    load_recent_messages,
    load_turn_pair_at_index,
    set_summary,
)
from app.services.ingest import get_vector_store
from app.services.recommender import filters_to_applied, synthesize_recommendations
from app.services.retriever import retrieve_games
from app.services.search import autocomplete_games, search_games
from app.state import app_state

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    if app_state.indexing_ok and app_state.indexed_games > 0:
        status = "degraded" if app_state.index_stale else "ok"
        return HealthResponse(status=status, indexed_games=app_state.indexed_games)
    return HealthResponse(status="degraded", indexed_games=app_state.indexed_games)


@router.post("/conversations", response_model=ConversationCreateResponse, status_code=201)
def create_conversation_endpoint(
    request: ConversationCreateRequest | None = None,
) -> ConversationCreateResponse:
    body = request or ConversationCreateRequest()
    session_factory = get_session_factory()
    with session_factory() as session:
        conv = create_conversation(session, title=body.title)
        session.commit()
        return ConversationCreateResponse(id=conv.id)


@router.post("/recommend", response_model=RecommendResponse)
def recommend(request: RecommendRequest) -> RecommendResponse:
    logger.info(
        "POST /recommend query=%r conversation_id=%s",
        request.query,
        request.conversation_id,
    )
    if not app_state.indexing_ok or app_state.indexed_games == 0:
        raise HTTPException(status_code=503, detail={"error": "No games indexed"})

    settings = app_state.settings
    llm = app_state.llm
    session_factory = get_session_factory()

    with session_factory() as session:
        conv = get_conversation(session, request.conversation_id)
        if conv is None:
            raise HTTPException(status_code=404, detail={"error": "Conversation not found"})
        epoch = conv.topic_started_at
        recent = load_recent_messages(
            session, request.conversation_id, topic_started_at=epoch
        )
        summary = conv.summary

    if recent and llm is None:
        raise HTTPException(status_code=502, detail={"error": "LLM unavailable"})

    try:
        plan = contextualize_query(
            llm,
            query=request.query,
            summary=summary,
            recent_messages=recent,
        )
    except (APIConnectionError, APIStatusError, ValidationError, json.JSONDecodeError) as exc:
        logger.exception("Contextualizer failed")
        raise HTTPException(status_code=502, detail={"error": "LLM unavailable"}) from exc

    standalone_query = plan.standalone_query
    filters = resolve_filters(llm, standalone_query)

    chroma_dir = Path(settings.chroma_persist_dir)
    vector_store = get_vector_store(chroma_dir, get_embeddings(settings))
    with session_factory() as session:
        candidates, filters_relaxed = retrieve_games(
            session, vector_store, filters, standalone_query, top_k=5
        )

    if llm is None:
        raise HTTPException(status_code=502, detail={"error": "LLM unavailable"})

    try:
        synthesis = synthesize_recommendations(
            llm, standalone_query, filters, candidates
        )
    except (APIConnectionError, APIStatusError) as exc:
        logger.exception("LLM provider unreachable during synthesis")
        raise HTTPException(status_code=502, detail={"error": "LLM unavailable"}) from exc

    applied = filters_to_applied(filters)
    response = RecommendResponse(
        recommendations=synthesis.recommendations,
        reasoning=synthesis.reasoning,
        filters_applied=applied,
        filters_relaxed=filters_relaxed,
        conversation_id=request.conversation_id,
        standalone_query=standalone_query,
        topic_changed=plan.topic_changed,
    )

    payload = {
        "reasoning": response.reasoning,
        "recommendations": [r.model_dump() for r in response.recommendations],
        "filters_applied": applied.model_dump(),
        "filters_relaxed": filters_relaxed,
        "standalone_query": standalone_query,
        "topic_changed": plan.topic_changed,
    }

    with session_factory() as session:
        append_turn(
            session,
            request.conversation_id,
            user_content=request.query,
            standalone_query=standalone_query,
            assistant_content=response.reasoning,
            assistant_payload=payload,
            topic_changed=plan.topic_changed,
        )
        session.commit()

        if not plan.topic_changed:
            turns = count_turns(
                session, request.conversation_id, topic_started_at=epoch
            )
            if turns > RECENT_TURN_LIMIT:
                dropped_index = turns - RECENT_TURN_LIMIT - 1
                pair = load_turn_pair_at_index(
                    session,
                    request.conversation_id,
                    dropped_index,
                    topic_started_at=epoch,
                )
                if pair is not None:
                    user_msg, assistant_msg = pair
                    try:
                        conv_after = get_conversation(session, request.conversation_id)
                        new_summary = summarize_dropped_turn(
                            llm,
                            prior_summary=conv_after.summary if conv_after else None,
                            user_content=user_msg.content,
                            assistant_content=assistant_msg.content,
                        )
                        set_summary(session, request.conversation_id, new_summary)
                        session.commit()
                    except Exception:
                        logger.exception("Summary refresh failed; keeping prior summary")

    logger.info(
        "POST /recommend complete recommendations=%d filters_relaxed=%s",
        len(response.recommendations),
        filters_relaxed,
    )
    return response


@router.post("/search", response_model=SearchResponse)
def search(request: SearchRequest) -> SearchResponse:
    logger.info("POST /search q=%r limit=%d", request.q, request.limit)
    session_factory = get_session_factory()
    with session_factory() as session:
        try:
            return search_games(session, request)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail={"error": "Invalid cursor"}
            ) from exc


@router.get("/search/autocomplete", response_model=AutocompleteResponse)
def autocomplete(
    q: str = Query(min_length=2),
    limit: int = Query(default=10, ge=1, le=20),
) -> AutocompleteResponse:
    logger.info("GET /search/autocomplete q=%r", q)
    session_factory = get_session_factory()
    with session_factory() as session:
        return autocomplete_games(session, q, limit)
