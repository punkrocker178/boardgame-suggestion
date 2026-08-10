#!/usr/bin/env python3
"""Import BGG rank CSV dump into PostgreSQL.

Imports are idempotent (upsert by game id). Commits run in batches so a crash
only loses the current uncommitted batch. Re-run the same command after a crash
to continue; already-committed rows are mostly skipped.
"""

from __future__ import annotations

import argparse
import csv
import logging
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.engine import get_session_factory, init_db
from app.db.models import CrawlStatus, Game
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)

_shutdown_requested = False


def _handle_sigint(_signum: int, _frame: object) -> None:
    global _shutdown_requested
    _shutdown_requested = True
    logger.warning("Shutdown requested; finishing current batch...")


@dataclass
class ImportStats:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0


def _int_or_none(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    return int(value)


def _float_or_none(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    return float(value)


def _parse_row(row: dict[str, str]) -> dict:
    return {
        "id": int(row["id"]),
        "name": row["name"].strip(),
        "year_published": _int_or_none(row.get("yearpublished")),
        "rank": _int_or_none(row.get("rank")),
        "bayes_average": _float_or_none(row.get("bayesaverage")),
        "average": _float_or_none(row.get("average")),
        "users_rated": _int_or_none(row.get("usersrated")),
        "is_expansion": row.get("is_expansion", "0").strip() == "1",
        "abstracts_rank": _int_or_none(row.get("abstracts_rank")),
        "cgs_rank": _int_or_none(row.get("cgs_rank")),
        "childrensgames_rank": _int_or_none(row.get("childrensgames_rank")),
        "familygames_rank": _int_or_none(row.get("familygames_rank")),
        "partygames_rank": _int_or_none(row.get("partygames_rank")),
        "strategygames_rank": _int_or_none(row.get("strategygames_rank")),
        "thematic_rank": _int_or_none(row.get("thematic_rank")),
        "wargames_rank": _int_or_none(row.get("wargames_rank")),
    }


def _count_csv_rows(csv_path: Path) -> int:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        total_lines = sum(1 for _ in handle)
    return max(total_lines - 1, 0)


def _upsert_row(session: Session, parsed: dict, stats: ImportStats) -> None:
    game_id = parsed["id"]
    existing = session.get(Game, game_id)

    if existing is None:
        session.add(
            Game(
                **parsed,
                crawl_status=CrawlStatus.PENDING,
            )
        )
        stats.inserted += 1
        return

    changed = False
    for field, value in parsed.items():
        if field == "id":
            continue
        if getattr(existing, field) != value:
            setattr(existing, field, value)
            changed = True

    if changed:
        stats.updated += 1
    else:
        stats.skipped += 1


def _commit_batch(
    session: Session,
    *,
    processed: int,
    total_rows: int,
    stats: ImportStats,
    progress_interval: int,
    last_logged: int,
) -> int:
    session.commit()
    if processed - last_logged >= progress_interval or processed == total_rows:
        logger.info(
            "Import progress: %d/%d (inserted=%d updated=%d skipped=%d)",
            processed,
            total_rows,
            stats.inserted,
            stats.updated,
            stats.skipped,
        )
        return processed
    return last_logged


def import_csv(
    session: Session,
    csv_path: Path,
    *,
    commit_batch_size: int = 1000,
    progress_interval: int | None = None,
    max_rows: int | None = None,
) -> ImportStats:
    if commit_batch_size < 1:
        raise ValueError("commit_batch_size must be at least 1")

    progress_every = progress_interval if progress_interval is not None else commit_batch_size
    stats = ImportStats()
    total_rows = _count_csv_rows(csv_path)
    if max_rows is not None:
        total_rows = min(total_rows, max_rows)

    processed = 0
    rows_since_commit = 0
    last_logged = 0

    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("CSV has no header row")

        for row in reader:
            if _shutdown_requested:
                break
            if max_rows is not None and processed >= max_rows:
                break

            _upsert_row(session, _parse_row(row), stats)
            processed += 1
            rows_since_commit += 1

            if rows_since_commit >= commit_batch_size:
                last_logged = _commit_batch(
                    session,
                    processed=processed,
                    total_rows=total_rows,
                    stats=stats,
                    progress_interval=progress_every,
                    last_logged=last_logged,
                )
                rows_since_commit = 0

    if rows_since_commit > 0:
        last_logged = _commit_batch(
            session,
            processed=processed,
            total_rows=total_rows,
            stats=stats,
            progress_interval=progress_every,
            last_logged=last_logged,
        )

    if _shutdown_requested:
        logger.warning(
            "Import stopped early at %d/%d rows (inserted=%d updated=%d skipped=%d)",
            processed,
            total_rows,
            stats.inserted,
            stats.updated,
            stats.skipped,
        )

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Import BGG rank CSV dump into PostgreSQL")
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("boardgames_ranks.csv"),
        help="Path to boardgames_ranks.csv",
    )
    parser.add_argument(
        "--commit-batch-size",
        type=int,
        default=1000,
        help="Commit to the database every N rows (default: 1000)",
    )
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=None,
        help="Log progress every N rows (default: same as --commit-batch-size)",
    )
    args = parser.parse_args()

    configure_logging(get_settings().log_level)
    signal.signal(signal.SIGINT, _handle_sigint)

    if not args.csv.exists():
        logger.error("CSV file not found: %s", args.csv)
        return 1

    init_db()
    session_factory = get_session_factory()

    total_rows = _count_csv_rows(args.csv)
    logger.info("Starting import of %d rows from %s", total_rows, args.csv)

    with session_factory() as session:
        stats = import_csv(
            session,
            args.csv,
            commit_batch_size=args.commit_batch_size,
            progress_interval=args.progress_interval,
        )

    logger.info(
        "Import complete: inserted=%d updated=%d skipped=%d",
        stats.inserted,
        stats.updated,
        stats.skipped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
