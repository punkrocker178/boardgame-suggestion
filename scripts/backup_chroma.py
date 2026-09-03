#!/usr/bin/env python3
"""Backup live Chroma persist dir to a gzip tarball."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tarfile
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.ingest import WATERMARK_FILENAME
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)

ARCHIVE_ROOT = "chroma"


class ChromaArchiveError(Exception):
    pass


def tarball_path(persist_dir: Path) -> Path:
    persist_dir = persist_dir.resolve()
    return persist_dir.parent / f"{persist_dir.name}.tar.gz"


def backup_chroma(persist_dir: Path, dest: Path | None = None) -> Path:
    persist_dir = persist_dir.resolve()
    watermark = persist_dir / WATERMARK_FILENAME
    if not persist_dir.is_dir() or not watermark.is_file():
        raise ChromaArchiveError(
            f"Live Chroma missing or has no {WATERMARK_FILENAME}: {persist_dir}"
        )
    dest = (dest or tarball_path(persist_dir)).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=dest.name + ".", suffix=".tmp", dir=dest.parent
    )
    tmp_path = Path(tmp_name)
    try:
        os.close(fd)
        with tarfile.open(tmp_path, "w:gz") as tar:
            tar.add(persist_dir, arcname=ARCHIVE_ROOT)
        tmp_path.replace(dest)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    logger.info("Wrote Chroma backup %s", dest)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backup live Chroma index to a gzip tarball"
    )
    parser.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level)
    persist = Path(settings.chroma_persist_dir)
    try:
        backup_chroma(persist)
    except ChromaArchiveError as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
