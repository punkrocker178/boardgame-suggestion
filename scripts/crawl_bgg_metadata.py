#!/usr/bin/env python3
"""Crawl BGG XML API 2 metadata for games in PostgreSQL."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bgg.parser import parse_thing_response
from app.config import get_settings
from app.db.engine import get_session_factory, init_db
from app.db.models import CrawlStatus, Game, GameCategory, GameMechanic
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)

BGG_THING_URL = "https://boardgamegeek.com/xmlapi2/thing"

_shutdown_requested = False


def _handle_sigint(_signum: int, _frame: object) -> None:
    global _shutdown_requested
    _shutdown_requested = True
    logger.warning("Shutdown requested; finishing current batch...")


@dataclass
class CrawlStats:
    batches: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0


def _build_query(
    session: Session,
    *,
    batch_size: int,
    include_unranked: bool,
    include_expansions: bool,
    reset_failed: bool,
) -> list[Game]:
    if reset_failed:
        failed_games = session.scalars(
            select(Game).where(Game.crawl_status == CrawlStatus.FAILED)
        ).all()
        for game in failed_games:
            game.crawl_status = CrawlStatus.PENDING
            game.last_crawl_error = None
        session.commit()

    stmt = select(Game).where(Game.crawl_status == CrawlStatus.PENDING)

    if not include_expansions:
        stmt = stmt.where(Game.is_expansion.is_(False))

    if not include_unranked:
        stmt = stmt.where(Game.rank.is_not(None), Game.rank > 0)

    stmt = stmt.order_by(Game.rank.asc().nulls_last(), Game.id.asc()).limit(batch_size)
    return list(session.scalars(stmt).all())


def _fetch_batch(
    client: httpx.Client,
    game_ids: list[int],
    *,
    max_retries: int,
) -> str:
    params = {"id": ",".join(str(game_id) for game_id in game_ids), "stats": "1"}
    backoff = 5.0

    for attempt in range(1, max_retries + 1):
        response = client.get(BGG_THING_URL, params=params)

        if response.status_code == 200:
            return response.text

        if response.status_code in (401, 403):
            raise RuntimeError(
                f"BGG API authentication failed ({response.status_code}). "
                "Check BGG_API_TOKEN and ensure requests go to boardgamegeek.com without www."
            )

        if response.status_code in (500, 503):
            logger.warning(
                "BGG API busy (%s), retry %d/%d in %.0fs",
                response.status_code,
                attempt,
                max_retries,
                backoff,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 120.0)
            continue

        response.raise_for_status()

    raise RuntimeError(f"BGG API failed after {max_retries} retries")


def _apply_thing_data(session: Session, game: Game, thing_data) -> None:
    game.description = thing_data.description
    game.min_players = thing_data.min_players
    game.max_players = thing_data.max_players
    game.playing_time = thing_data.playing_time
    game.min_play_time = thing_data.min_play_time
    game.max_play_time = thing_data.max_play_time
    game.min_age = thing_data.min_age
    game.weight = thing_data.weight
    game.thumbnail_url = thing_data.thumbnail_url
    game.image_url = thing_data.image_url

    game.categories.clear()
    for category in thing_data.categories:
        game.categories.append(GameCategory(category=category))

    game.mechanics.clear()
    for mechanic in thing_data.mechanics:
        game.mechanics.append(GameMechanic(mechanic=mechanic))


def _process_batch(
    session: Session,
    client: httpx.Client,
    games: list[Game],
    *,
    max_retries: int,
    stats: CrawlStats,
) -> None:
    game_ids = [game.id for game in games]
    xml_text = _fetch_batch(client, game_ids, max_retries=max_retries)
    parsed = parse_thing_response(xml_text)
    now = datetime.now(UTC)

    for game in games:
        game.crawl_attempts += 1
        thing_data = parsed.get(game.id)

        if thing_data is None:
            game.crawl_status = CrawlStatus.SKIPPED
            game.crawled_at = now
            game.last_crawl_error = "Not returned by BGG API"
            stats.skipped += 1
            continue

        _apply_thing_data(session, game, thing_data)
        game.crawl_status = CrawlStatus.COMPLETED
        game.crawled_at = now
        game.last_crawl_error = None
        stats.completed += 1

    session.commit()
    stats.batches += 1


def crawl(
    session: Session,
    *,
    batch_size: int,
    delay: float,
    max_batches: int | None,
    max_retries: int,
    max_attempts: int,
    include_unranked: bool,
    include_expansions: bool,
    reset_failed: bool,
) -> CrawlStats:
    settings = get_settings()
    if not settings.bgg_api_token:
        raise RuntimeError("BGG_API_TOKEN is required")

    stats = CrawlStats()
    headers = {"Authorization": f"Bearer {settings.bgg_api_token}"}

    with httpx.Client(headers=headers, timeout=30.0) as client:
        while not _shutdown_requested:
            if max_batches is not None and stats.batches >= max_batches:
                logger.info("Reached max batches (%d), stopping", max_batches)
                break

            games = _build_query(
                session,
                batch_size=batch_size,
                include_unranked=include_unranked,
                include_expansions=include_expansions,
                reset_failed=reset_failed and stats.batches == 0,
            )
            reset_failed = False

            if not games:
                logger.info("No pending games to crawl")
                break

            logger.info(
                "Crawling batch %d: %d games (ids %d..%d)",
                stats.batches + 1,
                len(games),
                games[0].id,
                games[-1].id,
            )

            try:
                _process_batch(
                    session,
                    client,
                    games,
                    max_retries=max_retries,
                    stats=stats,
                )
            except Exception as exc:
                logger.error("Batch failed: %s", exc)
                for game in games:
                    game.crawl_attempts += 1
                    game.last_crawl_error = str(exc)
                    if game.crawl_attempts >= max_attempts:
                        game.crawl_status = CrawlStatus.FAILED
                        stats.failed += 1
                session.commit()
                time.sleep(delay)
                continue

            logger.info(
                "Batch complete: completed=%d skipped=%d failed=%d",
                stats.completed,
                stats.skipped,
                stats.failed,
            )
            time.sleep(delay)

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Crawl BGG metadata into PostgreSQL")
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--delay", type=float, default=None)
    parser.add_argument("--max-batches", type=int, default=None)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--include-unranked", action="store_true")
    parser.add_argument("--include-expansions", action="store_true")
    parser.add_argument("--reset-failed", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    signal.signal(signal.SIGINT, _handle_sigint)

    batch_size = args.batch_size or settings.bgg_batch_size
    delay = args.delay if args.delay is not None else settings.bgg_request_delay_seconds

    init_db()
    session_factory = get_session_factory()

    try:
        with session_factory() as session:
            stats = crawl(
                session,
                batch_size=batch_size,
                delay=delay,
                max_batches=args.max_batches,
                max_retries=args.max_retries,
                max_attempts=args.max_attempts,
                include_unranked=args.include_unranked,
                include_expansions=args.include_expansions,
                reset_failed=args.reset_failed,
            )
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    logger.info(
        "Crawl finished: batches=%d completed=%d skipped=%d failed=%d",
        stats.batches,
        stats.completed,
        stats.skipped,
        stats.failed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
