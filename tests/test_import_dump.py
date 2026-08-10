from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.db.models import CrawlStatus, Game
from scripts.import_bgg_dump import import_csv


@pytest.fixture
def sample_dump_csv(tmp_path: Path) -> Path:
    path = tmp_path / "boardgames_ranks.csv"
    path.write_text(
        "id,name,yearpublished,rank,bayesaverage,average,usersrated,is_expansion,"
        "abstracts_rank,cgs_rank,childrensgames_rank,familygames_rank,"
        "partygames_rank,strategygames_rank,thematic_rank,wargames_rank\n"
        '1,"Game One",2020,10,7.5,7.8,1000,0,,,,,,1,,\n'
        '2,"Game Two",2019,20,7.2,7.4,500,0,,,,,,2,,\n'
        '3,"Expansion One",2021,0,6.5,6.8,100,1,,,,,,,,\n'
    )
    return path


def test_import_inserts_new_games(db_session: Session, sample_dump_csv: Path) -> None:
    stats = import_csv(db_session, sample_dump_csv)

    assert stats.inserted == 3
    assert stats.updated == 0
    assert stats.skipped == 0

    game = db_session.get(Game, 1)
    assert game is not None
    assert game.name == "Game One"
    assert game.rank == 10
    assert game.crawl_status == CrawlStatus.PENDING
    assert game.is_expansion is False

    expansion = db_session.get(Game, 3)
    assert expansion is not None
    assert expansion.is_expansion is True


def test_import_updates_without_resetting_crawl_status(
    db_session: Session, sample_dump_csv: Path
) -> None:
    import_csv(db_session, sample_dump_csv)

    game = db_session.get(Game, 1)
    assert game is not None
    game.crawl_status = CrawlStatus.COMPLETED
    db_session.commit()

    sample_dump_csv.write_text(
        "id,name,yearpublished,rank,bayesaverage,average,usersrated,is_expansion,"
        "abstracts_rank,cgs_rank,childrensgames_rank,familygames_rank,"
        "partygames_rank,strategygames_rank,thematic_rank,wargames_rank\n"
        '1,"Game One Updated",2020,5,7.6,7.9,1100,0,,,,,,1,,\n'
    )

    stats = import_csv(db_session, sample_dump_csv)
    db_session.refresh(game)

    assert stats.inserted == 0
    assert stats.updated == 1
    assert game.name == "Game One Updated"
    assert game.rank == 5
    assert game.crawl_status == CrawlStatus.COMPLETED


def test_import_commits_in_batches(db_session: Session, sample_dump_csv: Path) -> None:
    stats = import_csv(db_session, sample_dump_csv, commit_batch_size=2)

    assert stats.inserted == 3
    assert db_session.get(Game, 1) is not None
    assert db_session.get(Game, 2) is not None
    assert db_session.get(Game, 3) is not None


def test_import_partial_survives_simulated_crash(
    db_session: Session, sample_dump_csv: Path
) -> None:
    import_csv(db_session, sample_dump_csv, commit_batch_size=2, max_rows=2)
    db_session.expire_all()

    assert db_session.get(Game, 1) is not None
    assert db_session.get(Game, 2) is not None
    assert db_session.get(Game, 3) is None
