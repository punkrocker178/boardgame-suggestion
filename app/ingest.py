import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.bgg.parser import complexity_from_weight
from app.db.models import CrawlStatus, Game

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

logger = logging.getLogger(__name__)


class IngestError(Exception):
    pass


@dataclass
class IngestResult:
    indexed_count: int
    skipped: bool


def _document_text(row: dict[str, str]) -> str:
    return f"{row['name']}. {row['description']}. Categories: {row['categories']}."


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
    )


def _eligible_games_stmt():
    return (
        select(Game)
        .options(selectinload(Game.categories))
        .where(*_eligible_games_filters())
        .order_by(Game.rank.asc().nulls_last(), Game.name.asc())
    )


def _game_to_row(game: Game) -> dict[str, str]:
    description = game.description or game.name
    categories = ",".join(
        category.category.lower().replace(" ", "_") for category in game.categories
    )
    complexity = complexity_from_weight(
        float(game.weight) if game.weight is not None else None
    )
    row = {
        "name": game.name,
        "description": description,
        "min_players": str(game.min_players),
        "max_players": str(game.max_players),
        "play_time_minutes": str(game.playing_time),
        "categories": categories or "strategy",
    }
    if complexity:
        row["complexity"] = complexity
    return row


def load_games_for_rag(session: Session) -> list[dict[str, str]]:
    games = session.scalars(_eligible_games_stmt()).all()
    if not games:
        raise IngestError(
            "No eligible games in database; run import + crawl before starting the API"
        )
    rows: list[dict[str, str]] = []
    for index, game in enumerate(games, start=1):
        row = _game_to_row(game)
        validate_row(row, index)
        rows.append(row)
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
        complexity = row.get("complexity", "").strip()
        if complexity:
            metadata["complexity"] = complexity

        documents.append(
            Document(page_content=_document_text(row), metadata=metadata)
        )
    return documents


def _watermark_path(chroma_dir: Path) -> Path:
    return chroma_dir / WATERMARK_FILENAME


def ingest_games(
    session: Session,
    chroma_dir: Path,
    embeddings: Embeddings,
    *,
    force: bool = False,
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
        return IngestResult(indexed_count=len(rows), skipped=True)

    logger.info("Indexing games from database into %s", chroma_dir)
    rows = load_games_for_rag(session)
    documents = rows_to_documents(rows)

    Chroma.from_documents(
        documents=documents,
        embedding=embeddings,
        collection_name=COLLECTION_NAME,
        persist_directory=str(chroma_dir),
    )

    watermark_file.write_text(current_watermark)
    logger.info(
        "Indexed %d games into Chroma collection %s",
        len(documents),
        COLLECTION_NAME,
    )
    return IngestResult(indexed_count=len(documents), skipped=False)


def get_vector_store(chroma_dir: Path, embeddings: Embeddings) -> Chroma:
    logger.debug("Opening Chroma store at %s (collection=%s)", chroma_dir, COLLECTION_NAME)
    return Chroma(
        collection_name=COLLECTION_NAME,
        persist_directory=str(chroma_dir),
        embedding_function=embeddings,
    )
