import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from openai import APIConnectionError, APIStatusError, RateLimitError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.bgg.parser import complexity_from_weight
from app.db.models import CrawlStatus, Game, GameCategory, GameMechanic

REQUIRED_COLUMNS = [
    "name",
    "description",
    "min_players",
    "max_players",
    "play_time_minutes",
    "categories",
]
ALLOWED_COMPLEXITY = {"light", "medium", "heavy"}
COLLECTION_NAME = "board_games"
WATERMARK_FILENAME = ".games_db_watermark"
DEFAULT_EMBED_BATCH_SIZE = 200
DEFAULT_EMBED_MAX_RETRIES = 5
DEFAULT_EMBED_RETRY_CAP_SECONDS = 60
DEFAULT_EMBED_REQUEST_DELAY_SECONDS = 0.0

logger = logging.getLogger(__name__)


class IngestError(Exception):
    pass


class IngestCancelled(BaseException):
    """Shutdown requested during ingest. Staging is left as a checkpoint."""


@dataclass
class IngestResult:
    indexed_count: int
    skipped: bool
    stale: bool = False


def encode_player_list(players: list[int] | None) -> str | None:
    if not players:
        return None
    return "#" + "#".join(str(int(p)) for p in players) + "#"


def _document_text(row: dict[str, str]) -> str:
    text = f"{row['name']}. {row['description']}. Categories: {row['categories']}."
    mechanics = (row.get("mechanics") or "").strip()
    if mechanics:
        text += f" Mechanics: {mechanics}."
    return text


def _parse_categories(raw: str) -> list[str]:
    return [c.strip() for c in raw.split(",") if c.strip()]


def validate_row(row: dict[str, str], line_number: int) -> None:
    for col in REQUIRED_COLUMNS:
        if not row.get(col, "").strip():
            raise IngestError(f"Line {line_number}: missing required column '{col}'")

    try:
        min_players = int(row["min_players"])
        max_players = int(row["max_players"])
        play_time = int(row["play_time_minutes"])
    except ValueError as exc:
        raise IngestError(f"Line {line_number}: invalid integer value") from exc

    if min_players < 1 or max_players < min_players:
        raise IngestError(f"Line {line_number}: invalid player count range")
    if play_time < 1:
        raise IngestError(f"Line {line_number}: play_time_minutes must be positive")

    complexity = row.get("complexity", "").strip()
    if complexity and complexity not in ALLOWED_COMPLEXITY:
        raise IngestError(f"Line {line_number}: invalid complexity '{complexity}'")


def _eligible_games_filters():
    return (
        Game.crawl_status == CrawlStatus.COMPLETED,
        Game.is_expansion.is_(False),
        Game.min_players.is_not(None),
        Game.max_players.is_not(None),
        Game.playing_time.is_not(None),
        Game.rank > 0,
    )


def _eligible_games_stmt():
    return (
        select(Game)
        .options(
            selectinload(Game.categories).selectinload(GameCategory.category),
            selectinload(Game.mechanics).selectinload(GameMechanic.mechanic),
        )
        .where(*_eligible_games_filters())
        .order_by(Game.rank.asc().nulls_last(), Game.name.asc())
    )


def _game_to_row(game: Game) -> dict[str, str]:
    description = game.description or game.name
    categories = ",".join(
        link.category.name.lower().replace(" ", "_") for link in game.categories
    )
    mechanics = ",".join(
        link.mechanic.name.lower().replace(" ", "_") for link in game.mechanics
    )
    complexity = complexity_from_weight(
        float(game.weight) if game.weight is not None else None
    )
    row = {
        "id": str(game.id),
        "name": game.name,
        "description": description,
        "min_players": str(game.min_players),
        "max_players": str(game.max_players),
        "play_time_minutes": str(game.playing_time),
        "categories": categories or "strategy",
        "mechanics": mechanics,
    }
    if complexity:
        row["complexity"] = complexity
    if game.weight is not None:
        row["weight"] = str(float(game.weight))
    if game.min_age is not None:
        row["min_age"] = str(game.min_age)
    if game.year_published is not None:
        row["year_published"] = str(game.year_published)
    best_with = encode_player_list(game.best_with_players)
    if best_with:
        row["best_with_players"] = best_with
    recommended_with = encode_player_list(game.recommended_with_players)
    if recommended_with:
        row["recommended_with_players"] = recommended_with
    return row


def load_games_for_rag(session: Session) -> list[dict[str, str]]:
    games = session.scalars(_eligible_games_stmt()).all()
    if not games:
        raise IngestError(
            "No eligible games in database; run import + crawl before starting the API"
        )
    rows: list[dict[str, str]] = []
    skipped = 0
    for index, game in enumerate(games, start=1):
        row = _game_to_row(game)
        try:
            validate_row(row, index)
        except IngestError as exc:
            skipped += 1
            logger.warning(
                "Skipping game id=%s name=%r: %s",
                game.id,
                game.name,
                exc,
            )
            continue
        rows.append(row)
    if not rows:
        raise IngestError(
            "No valid games to index after skipping invalid rows; "
            "check player counts and play times in crawled data"
        )
    if skipped:
        logger.warning(
            "Skipped %d invalid games; indexing %d valid games", skipped, len(rows)
        )
    else:
        logger.debug("Loaded %d eligible games from database", len(rows))
    return rows


def compute_games_watermark(session: Session) -> str:
    filters = _eligible_games_filters()
    count = session.scalar(select(func.count()).select_from(Game).where(*filters))
    max_updated = session.scalar(select(func.max(Game.updated_at)).where(*filters))
    if isinstance(max_updated, datetime):
        stamp = max_updated.isoformat()
    else:
        stamp = ""
    return f"{int(count or 0)}:{stamp}"


def rows_to_documents(rows: list[dict[str, str]]) -> list[Document]:
    documents: list[Document] = []
    for row in rows:
        categories = _parse_categories(row["categories"])
        metadata: dict = {
            "name": row["name"],
            "min_players": int(row["min_players"]),
            "max_players": int(row["max_players"]),
            "play_time_minutes": int(row["play_time_minutes"]),
            "categories": ",".join(categories),
        }
        if row.get("id"):
            metadata["game_id"] = int(row["id"])
        complexity = row.get("complexity", "").strip()
        if complexity:
            metadata["complexity"] = complexity
        if row.get("weight"):
            metadata["weight"] = float(row["weight"])
        if row.get("min_age"):
            metadata["min_age"] = int(row["min_age"])
        if row.get("year_published"):
            metadata["year_published"] = int(row["year_published"])
        if row.get("best_with_players"):
            metadata["best_with_players"] = row["best_with_players"]
        if row.get("recommended_with_players"):
            metadata["recommended_with_players"] = row["recommended_with_players"]

        documents.append(
            Document(page_content=_document_text(row), metadata=metadata)
        )
    return documents


def _watermark_path(chroma_dir: Path) -> Path:
    return chroma_dir / WATERMARK_FILENAME


def _document_id(doc: Document) -> str:
    if "game_id" in doc.metadata:
        return str(doc.metadata["game_id"])
    return str(doc.metadata["name"])


def _staging_is_resumable(staging: Path, current_watermark: str) -> bool:
    watermark_file = _watermark_path(staging)
    try:
        return (
            staging.exists()
            and watermark_file.exists()
            and watermark_file.read_text().strip() == current_watermark
        )
    except OSError:
        return False


def _prepare_staging(
    staging: Path, current_watermark: str, *, force: bool
) -> bool:
    resume = (not force) and _staging_is_resumable(staging, current_watermark)
    if staging.exists() and not resume:
        shutil.rmtree(staging, ignore_errors=True)
    return resume and staging.exists()


def _collection_ids(store: Chroma) -> set[str]:
    try:
        result = store._collection.get()
        return set(result.get("ids") or [])
    except Exception:
        logger.debug("Could not read existing staging IDs", exc_info=True)
        return set()


def _staging_dir(chroma_dir: Path) -> Path:
    return chroma_dir.with_name(chroma_dir.name + "_staging")


def _old_dir(chroma_dir: Path) -> Path:
    return chroma_dir.with_name(chroma_dir.name + "_old")


def _try_rmtree(path: Path) -> bool:
    try:
        shutil.rmtree(path)
        return True
    except OSError:
        logger.warning("Could not remove %s", path, exc_info=True)
        return False


def _unique_old_dir(live: Path) -> Path:
    candidate = _old_dir(live)
    if not candidate.exists():
        return candidate
    pid_dir = live.with_name(f"{live.name}_old.{os.getpid()}")
    if not pid_dir.exists():
        return pid_dir
    return live.with_name(f"{live.name}_old.{os.getpid()}.{time.time_ns()}")


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise IngestCancelled()


def _interruptible_sleep(
    seconds: float, cancel: threading.Event | None
) -> None:
    if seconds <= 0:
        return
    if cancel is None:
        time.sleep(seconds)
        return
    if cancel.wait(timeout=seconds):
        raise IngestCancelled()


def _is_retryable_embed_error(exc: BaseException) -> bool:
    if isinstance(exc, (APIConnectionError, RateLimitError, TimeoutError)):
        return True
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        if status == 429:
            return True
        if isinstance(status, int) and status >= 500:
            return True
    return False


def embed_documents_with_retry(
    embeddings: Embeddings,
    texts: list[str],
    *,
    max_retries: int = DEFAULT_EMBED_MAX_RETRIES,
    retry_cap_seconds: float = DEFAULT_EMBED_RETRY_CAP_SECONDS,
    cancel: threading.Event | None = None,
) -> list[list[float]]:
    attempt = 0
    while True:
        _check_cancel(cancel)
        try:
            return embeddings.embed_documents(texts)
        except IngestCancelled:
            raise
        except Exception as exc:
            if not _is_retryable_embed_error(exc) or attempt >= max_retries:
                raise
            delay = min(2**attempt, retry_cap_seconds)
            logger.warning(
                "Embedding batch failed (attempt %d/%d): %s; retrying in %.1fs",
                attempt + 1,
                max_retries,
                exc,
                delay,
            )
            _interruptible_sleep(delay, cancel)
            attempt += 1


def _clear_chroma_client_cache() -> None:
    """Drop cached PersistentClients so dirs can be renamed/removed safely."""
    try:
        from chromadb.api.client import SharedSystemClient

        SharedSystemClient.clear_system_cache()
    except Exception:
        logger.debug("Could not clear Chroma system cache", exc_info=True)


def count_indexed_games(chroma_dir: Path, embeddings: Embeddings) -> int:
    if not chroma_dir.exists():
        return 0
    try:
        store = get_vector_store(chroma_dir, embeddings)
        return int(store._collection.count())
    except Exception:
        logger.exception("Failed to count documents in live Chroma at %s", chroma_dir)
        return 0
    finally:
        _clear_chroma_client_cache()


def _swap_staging_to_live(staging: Path, live: Path) -> None:
    _clear_chroma_client_cache()
    old = _old_dir(live)
    if old.exists() and not _try_rmtree(old):
        old = _unique_old_dir(live)
    if live.exists():
        live.rename(old)
    try:
        staging.rename(live)
    except Exception:
        if not live.exists() and old.exists():
            old.rename(live)
        raise
    if old.exists():
        _try_rmtree(old)
    _clear_chroma_client_cache()


def _index_documents_to_dir(
    documents: list[Document],
    target_dir: Path,
    embeddings: Embeddings,
    *,
    batch_size: int,
    max_retries: int,
    request_delay: float = DEFAULT_EMBED_REQUEST_DELAY_SECONDS,
    resume: bool = False,
    cancel: threading.Event | None = None,
) -> None:
    _clear_chroma_client_cache()
    target_dir.mkdir(parents=True, exist_ok=True)

    store = Chroma(
        collection_name=COLLECTION_NAME,
        persist_directory=str(target_dir),
        embedding_function=embeddings,
    )
    try:
        existing = _collection_ids(store) if resume else set()
        pending = [doc for doc in documents if _document_id(doc) not in existing]
        for i, batch in enumerate(_chunks(pending, batch_size)):
            _check_cancel(cancel)
            if i and request_delay > 0:
                # ponytail: fixed sleep; upgrade: honor Retry-After / X-RateLimit-Reset
                _interruptible_sleep(request_delay, cancel)
            texts = [doc.page_content for doc in batch]
            metadatas = [doc.metadata for doc in batch]
            ids = [_document_id(doc) for doc in batch]
            vectors = embed_documents_with_retry(
                embeddings, texts, max_retries=max_retries, cancel=cancel
            )
            store._collection.upsert(
                ids=ids,
                documents=texts,
                metadatas=metadatas,
                embeddings=vectors,
            )
    finally:
        _clear_chroma_client_cache()


def ingest_games(
    session: Session,
    chroma_dir: Path,
    embeddings: Embeddings,
    *,
    force: bool = False,
    batch_size: int = DEFAULT_EMBED_BATCH_SIZE,
    max_retries: int = DEFAULT_EMBED_MAX_RETRIES,
    request_delay: float = DEFAULT_EMBED_REQUEST_DELAY_SECONDS,
    cancel: threading.Event | None = None,
) -> IngestResult:
    chroma_dir.mkdir(parents=True, exist_ok=True)
    current_watermark = compute_games_watermark(session)
    watermark_file = _watermark_path(chroma_dir)

    if (
        not force
        and watermark_file.exists()
        and watermark_file.read_text().strip() == current_watermark
    ):
        rows = load_games_for_rag(session)
        logger.info(
            "Skipping re-index; DB watermark unchanged (%d games)", len(rows)
        )
        return IngestResult(indexed_count=len(rows), skipped=True, stale=False)

    logger.info("Indexing games from database into staging for %s", chroma_dir)
    rows = load_games_for_rag(session)
    documents = rows_to_documents(rows)
    staging = _staging_dir(chroma_dir)
    resume = _prepare_staging(staging, current_watermark, force=force)
    staging.mkdir(parents=True, exist_ok=True)
    _watermark_path(staging).write_text(current_watermark)

    try:
        _index_documents_to_dir(
            documents,
            staging,
            embeddings,
            batch_size=batch_size,
            max_retries=max_retries,
            request_delay=request_delay,
            resume=resume,
            cancel=cancel,
        )
        _swap_staging_to_live(staging, chroma_dir)
        watermark_file.write_text(current_watermark)
    except IngestCancelled:
        logger.info("Indexing cancelled; staging checkpoint kept at %s", staging)
        raise
    except Exception:
        live_count = count_indexed_games(chroma_dir, embeddings)
        if live_count > 0:
            logger.exception(
                "Re-index failed; keeping live Chroma with %d documents", live_count
            )
            return IngestResult(
                indexed_count=live_count, skipped=False, stale=True
            )
        raise

    logger.info(
        "Indexed %d games into Chroma collection %s",
        len(documents),
        COLLECTION_NAME,
    )
    return IngestResult(indexed_count=len(documents), skipped=False, stale=False)


def get_vector_store(chroma_dir: Path, embeddings: Embeddings) -> Chroma:
    logger.debug("Opening Chroma store at %s (collection=%s)", chroma_dir, COLLECTION_NAME)
    return Chroma(
        collection_name=COLLECTION_NAME,
        persist_directory=str(chroma_dir),
        embedding_function=embeddings,
    )
