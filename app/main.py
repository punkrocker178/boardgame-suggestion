import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from langchain_core.language_models import BaseChatModel
from openai import APIConnectionError, APIStatusError

from app.config import Settings, get_embeddings, get_llm, get_settings
from app.db.engine import get_session_factory
from app.ingest import IngestError, get_vector_store, ingest_games
from app.logging_config import configure_logging
from app.models import HealthResponse, RecommendRequest, RecommendResponse
from app.query_extractor import extract_filters
from app.recommender import filters_to_applied, synthesize_recommendations
from app.retriever import retrieve_games

logger = logging.getLogger(__name__)


class AppState:
    def __init__(self) -> None:
        self.settings: Settings = get_settings()
        self.indexed_games: int = 0
        self.indexing_ok: bool = False
        self.llm: BaseChatModel | None = None


app_state = AppState()


def _run_indexing(settings: Settings) -> None:
    chroma_dir = Path(settings.chroma_persist_dir)
    embeddings = get_embeddings(settings)
    session_factory = get_session_factory()
    with session_factory() as session:
        result = ingest_games(session, chroma_dir, embeddings)
    app_state.indexed_games = result.indexed_count
    app_state.indexing_ok = True
    if result.skipped:
        logger.info(
            "Skipped re-index; DB watermark unchanged (%d games)",
            result.indexed_count,
        )
    else:
        logger.info("Indexed %d games into Chroma", result.indexed_count)


@asynccontextmanager
async def lifespan(_: FastAPI):
    app_state.settings = get_settings()
    configure_logging(app_state.settings.log_level)
    logger.info("Starting Board Game RAG Game Master")
    app_state.llm = get_llm(app_state.settings)
    try:
        _run_indexing(app_state.settings)
    except IngestError:
        logger.exception("Failed to index games from database")
        app_state.indexing_ok = False
        app_state.indexed_games = 0
        raise
    yield


app = FastAPI(title="Board Game RAG Game Master", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    if app_state.indexing_ok and app_state.indexed_games > 0:
        return HealthResponse(status="ok", indexed_games=app_state.indexed_games)
    return HealthResponse(status="degraded", indexed_games=app_state.indexed_games)


@app.post("/recommend", response_model=RecommendResponse)
def recommend(request: RecommendRequest) -> RecommendResponse:
    logger.info("POST /recommend query=%r session_id=%s", request.query, request.session_id)
    if not app_state.indexing_ok or app_state.indexed_games == 0:
        raise HTTPException(status_code=503, detail={"error": "No games indexed"})

    settings = app_state.settings
    llm = app_state.llm
    if llm is None:
        raise HTTPException(status_code=502, detail={"error": "LLM unavailable"})

    try:
        filters = extract_filters(llm, request.query)
    except (APIConnectionError, APIStatusError) as exc:
        logger.exception("LLM provider unreachable during extraction")
        raise HTTPException(status_code=502, detail={"error": "LLM unavailable"}) from exc

    chroma_dir = Path(settings.chroma_persist_dir)
    vector_store = get_vector_store(chroma_dir, get_embeddings(settings))
    candidates, filters_relaxed = retrieve_games(
        vector_store, filters, request.query, top_k=5
    )

    try:
        synthesis = synthesize_recommendations(llm, request.query, filters, candidates)
    except (APIConnectionError, APIStatusError) as exc:
        logger.exception("LLM provider unreachable during synthesis")
        raise HTTPException(status_code=502, detail={"error": "LLM unavailable"}) from exc

    response = RecommendResponse(
        recommendations=synthesis.recommendations,
        reasoning=synthesis.reasoning,
        filters_applied=filters_to_applied(filters),
        filters_relaxed=filters_relaxed,
    )
    logger.info(
        "POST /recommend complete recommendations=%d filters_relaxed=%s",
        len(response.recommendations),
        filters_relaxed,
    )
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
