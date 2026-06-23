import csv
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

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
HASH_FILENAME = ".games_csv_hash"

logger = logging.getLogger(__name__)


class IngestError(Exception):
    pass


@dataclass
class IngestResult:
    indexed_count: int
    skipped: bool


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash_path(chroma_dir: Path) -> Path:
    return chroma_dir / HASH_FILENAME


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


def load_games_csv(csv_path: Path) -> list[dict[str, str]]:
    if not csv_path.exists():
        raise IngestError(f"CSV file not found: {csv_path}")

    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise IngestError("CSV has no header row")

        missing = set(REQUIRED_COLUMNS) - set(reader.fieldnames)
        if missing:
            raise IngestError(f"CSV missing required columns: {sorted(missing)}")

        rows: list[dict[str, str]] = []
        for line_number, row in enumerate(reader, start=2):
            validate_row(row, line_number)
            rows.append(row)

    if not rows:
        raise IngestError("CSV contains no game rows")

    logger.debug("Loaded %d games from %s", len(rows), csv_path)
    return rows


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


def ingest_games(
    csv_path: Path,
    chroma_dir: Path,
    embeddings: Embeddings,
    *,
    force: bool = False,
) -> IngestResult:
    chroma_dir.mkdir(parents=True, exist_ok=True)
    current_hash = _hash_file(csv_path)
    hash_file = _hash_path(chroma_dir)

    if not force and hash_file.exists() and hash_file.read_text().strip() == current_hash:
        rows = load_games_csv(csv_path)
        logger.info(
            "Skipping re-index for %s; hash unchanged (%d games)", csv_path, len(rows)
        )
        return IngestResult(indexed_count=len(rows), skipped=True)

    logger.info("Indexing games from %s into %s", csv_path, chroma_dir)
    rows = load_games_csv(csv_path)
    documents = rows_to_documents(rows)

    Chroma.from_documents(
        documents=documents,
        embedding=embeddings,
        collection_name=COLLECTION_NAME,
        persist_directory=str(chroma_dir),
    )

    hash_file.write_text(current_hash)
    logger.info("Indexed %d games into Chroma collection %s", len(documents), COLLECTION_NAME)
    return IngestResult(indexed_count=len(documents), skipped=False)


def get_vector_store(chroma_dir: Path, embeddings: Embeddings) -> Chroma:
    logger.debug("Opening Chroma store at %s (collection=%s)", chroma_dir, COLLECTION_NAME)
    return Chroma(
        collection_name=COLLECTION_NAME,
        persist_directory=str(chroma_dir),
        embedding_function=embeddings,
    )
