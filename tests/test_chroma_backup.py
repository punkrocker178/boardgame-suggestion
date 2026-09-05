import io
import tarfile
from pathlib import Path

import pytest

from app.services.ingest import WATERMARK_FILENAME
from scripts.backup_chroma import ChromaArchiveError, backup_chroma, tarball_path
from scripts.restore_chroma import restore_chroma


def _live_dir(tmp_path: Path, *, watermark: str | None = "1:stamp") -> Path:
    live = tmp_path / "chroma"
    live.mkdir()
    (live / "chroma.sqlite3").write_text("sqlite")
    if watermark is not None:
        (live / WATERMARK_FILENAME).write_text(watermark)
    return live


def test_tarball_path_uses_persist_dir_name(tmp_path: Path) -> None:
    persist = tmp_path / "chroma"
    assert tarball_path(persist) == tmp_path / "chroma.tar.gz"


def test_backup_writes_chroma_root_and_watermark(tmp_path: Path) -> None:
    live = _live_dir(tmp_path)
    dest = backup_chroma(live)
    assert dest == tmp_path / "chroma.tar.gz"
    assert dest.is_file()
    with tarfile.open(dest, "r:gz") as tar:
        names = tar.getnames()
        assert "chroma/.games_db_watermark" in names
        assert any(n == "chroma/chroma.sqlite3" or n.endswith("/chroma.sqlite3") for n in names)
        member = tar.extractfile("chroma/.games_db_watermark")
        assert member is not None
        assert member.read().decode() == "1:stamp"


def test_backup_without_watermark_fails_and_leaves_existing_tarball(
    tmp_path: Path,
) -> None:
    live = _live_dir(tmp_path, watermark=None)
    existing = tmp_path / "chroma.tar.gz"
    existing.write_text("keep-me")
    with pytest.raises(ChromaArchiveError):
        backup_chroma(live)
    assert existing.read_text() == "keep-me"


def test_backup_missing_dir_fails(tmp_path: Path) -> None:
    with pytest.raises(ChromaArchiveError):
        backup_chroma(tmp_path / "chroma")


def test_restore_replaces_live_and_leaves_staging(tmp_path: Path) -> None:
    live = _live_dir(tmp_path, watermark="backup-mark")
    staging = tmp_path / "chroma_staging"
    staging.mkdir()
    (staging / "keep.txt").write_text("staging")
    archive = backup_chroma(live)

    live.joinpath("chroma.sqlite3").write_text("dirty")
    restore_chroma(archive, live)

    assert (live / WATERMARK_FILENAME).read_text() == "backup-mark"
    assert (live / "chroma.sqlite3").read_text() == "sqlite"
    assert (staging / "keep.txt").read_text() == "staging"


def test_restore_without_watermark_leaves_live(tmp_path: Path) -> None:
    live = _live_dir(tmp_path, watermark="live-mark")
    archive = tmp_path / "chroma.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name="chroma/chroma.sqlite3")
        data = b"x"
        info.size = len(data)
        tar.addfile(info, fileobj=io.BytesIO(data))

    with pytest.raises(ChromaArchiveError):
        restore_chroma(archive, live)

    assert (live / WATERMARK_FILENAME).read_text() == "live-mark"
