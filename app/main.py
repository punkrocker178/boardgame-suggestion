import logging
import signal
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from openai import APIConnectionError, APIStatusError

from app.api.routes import router
from app.config import Settings, get_embeddings, get_llm, get_settings
from app.db.engine import get_session_factory
from app.logging_config import configure_logging
from app.services.ingest import IngestCancelled, IngestError, count_indexed_games, ingest_games
from app.state import app_state

logger = logging.getLogger(__name__)


def _chain_cancel_on_signals(cancel: threading.Event):
    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    previous: dict[int, object] = {}

    def _handler(signum: int, frame: object) -> None:
        cancel.set()
        prev = previous.get(signum)
        if callable(prev):
            prev(signum, frame)

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.getsignal(sig)
        signal.signal(sig, _handler)

    def restore() -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)  # type: ignore[arg-type]

    return restore


def _run_indexing(
    settings: Settings, cancel: threading.Event | None = None
) -> None:
    chroma_dir = Path(settings.chroma_persist_dir)
    embeddings = get_embeddings(settings)
    session_factory = get_session_factory()
    with session_factory() as session:
        result = ingest_games(
            session,
            chroma_dir,
            embeddings,
            batch_size=settings.embedding_batch_size,
            max_retries=settings.embedding_max_retries,
            request_delay=settings.embedding_request_delay_seconds,
            cancel=cancel,
        )
    app_state.indexed_games = result.indexed_count
    app_state.indexing_ok = result.indexed_count > 0
    app_state.index_stale = result.stale
    if result.skipped:
        logger.info(
            "Skipped re-index; DB watermark unchanged (%d games)",
            result.indexed_count,
        )
    elif result.stale:
        logger.warning(
            "Serving stale Chroma index (%d games) after failed refresh",
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
    cancel = threading.Event()
    restore_signals = _chain_cancel_on_signals(cancel)
    try:
        try:
            _run_indexing(app_state.settings, cancel)
        except IngestCancelled:
            logger.info("Indexing interrupted; exiting")
            raise
        except IngestError:
            logger.exception("Failed to index games from database")
            _fallback_to_live_or_down()
            if not app_state.indexing_ok:
                raise
        except (APIConnectionError, APIStatusError, OSError, RuntimeError):
            logger.exception("Indexing failed due to provider or runtime error")
            _fallback_to_live_or_down()
            if not app_state.indexing_ok:
                raise
        yield
    finally:
        restore_signals()


def _fallback_to_live_or_down() -> None:
    settings = app_state.settings
    chroma_dir = Path(settings.chroma_persist_dir)
    try:
        embeddings = get_embeddings(settings)
        live_count = count_indexed_games(chroma_dir, embeddings)
    except Exception:
        logger.exception("Could not inspect live Chroma after indexing failure")
        live_count = 0
    if live_count > 0:
        app_state.indexed_games = live_count
        app_state.indexing_ok = True
        app_state.index_stale = True
        logger.warning(
            "Starting with stale live Chroma index (%d games)", live_count
        )
        return
    app_state.indexing_ok = False
    app_state.indexed_games = 0
    app_state.index_stale = False


app = FastAPI(title="Board Game RAG Game Master", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
