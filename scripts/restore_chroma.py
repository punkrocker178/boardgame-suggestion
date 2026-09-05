#!/usr/bin/env python3
"""Restore live Chroma persist dir from a gzip tarball."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.services.ingest import WATERMARK_FILENAME
from app.logging_config import configure_logging
from scripts.backup_chroma import ARCHIVE_ROOT, ChromaArchiveError, tarball_path

logger = logging.getLogger(__name__)


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    prefix = ARCHIVE_ROOT + "/"
    members = []
    for member in tar.getmembers():
        name = member.name.replace("\\", "/")
        if name in (ARCHIVE_ROOT, prefix.rstrip("/")) or name.startswith(prefix):
            if ".." in Path(name).parts:
                raise ChromaArchiveError(f"Unsafe archive member: {member.name}")
            members.append(member)
    return members


def restore_chroma(archive: Path, persist_dir: Path) -> None:
    archive = archive.resolve()
    persist_dir = persist_dir.resolve()
    watermark_member = f"{ARCHIVE_ROOT}/{WATERMARK_FILENAME}"
    if not archive.is_file():
        raise ChromaArchiveError(f"Archive not found: {archive}")
    try:
        with tarfile.open(archive, "r:gz") as tar:
            names = {m.name.replace("\\", "/") for m in tar.getmembers()}
            if watermark_member not in names:
                raise ChromaArchiveError(
                    f"Archive missing {watermark_member}: {archive}"
                )
            members = _safe_members(tar)
            persist_dir.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=persist_dir.parent) as tmp:
                tar.extractall(tmp, members=members, filter="data")
                extracted = Path(tmp) / ARCHIVE_ROOT
                if not (extracted / WATERMARK_FILENAME).is_file():
                    raise ChromaArchiveError(
                        f"Archive missing {watermark_member}: {archive}"
                    )
                if persist_dir.exists():
                    shutil.rmtree(persist_dir)
                extracted.rename(persist_dir)
    except ChromaArchiveError:
        raise
    except (tarfile.TarError, OSError) as exc:
        raise ChromaArchiveError(f"Could not restore {archive}: {exc}") from exc
    logger.info("Restored Chroma index to %s", persist_dir)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore live Chroma index from gzip tarball"
    )
    parser.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level)
    persist = Path(settings.chroma_persist_dir)
    try:
        restore_chroma(tarball_path(persist), persist)
    except ChromaArchiveError as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
