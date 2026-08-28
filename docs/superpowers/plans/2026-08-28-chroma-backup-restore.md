# Chroma live index backup and restore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add backup/restore scripts that copy a finished live Chroma dir as a gzip tarball so a wipe or another machine can skip re-embedding when the DB watermark still matches.

**Architecture:** Stdlib `tarfile` of live `CHROMA_PERSIST_DIR` only, archive root always `chroma/`, path `<persist_dir.name>.tar.gz` next to the persist dir. Backup fails without `.games_db_watermark`. Restore validates the watermark member first, then replaces live; staging is never read or written. Ingest is unchanged.

**Tech Stack:** Python 3 stdlib (`tarfile`, `shutil`, `argparse`), existing `app.config.get_settings`, `app.ingest.WATERMARK_FILENAME`, pytest + `tmp_path`

**Spec:** `docs/superpowers/specs/2026-08-28-chroma-backup-restore-design.md`

## Global Constraints

- Backup live persist dir only; never include staging or `chroma_old`
- Archive layout: single top-level directory named `chroma/` (not the persist folder basename)
- Tarball path: `<persist_dir.parent>/<persist_dir.name>.tar.gz` (default `data/chroma.tar.gz`)
- Overwrite tarball via temp file then rename
- Missing live dir or missing `.games_db_watermark` → backup fails; existing tarball must not be replaced
- Restore of archive without `chroma/.games_db_watermark` fails; live dir must remain unchanged
- Restore must not read or write `chroma_staging`
- Scripts do not lock, kill, or detect a running API
- Do not change ingest, swap, or watermark formula
- Out of scope: auto-restore on startup, writing a watermark onto live that lacks one, staging in the archive

---

## File map

| File | Responsibility |
|------|----------------|
| `scripts/backup_chroma.py` | `tarball_path`, `ChromaArchiveError`, `backup_chroma`; CLI |
| `scripts/restore_chroma.py` | `restore_chroma`; CLI; imports shared error/path from backup script |
| `tests/test_chroma_backup.py` | Backup/restore tests |
| `docs/database.md` | Chroma backup/restore section |
| `README.md` | Point at Chroma backup in database docs |
| `.gitignore` | `data/chroma.tar.gz` |

---

### Task 1: Backup live Chroma to gzip tarball

**Files:**
- Create: `scripts/backup_chroma.py`
- Test: `tests/test_chroma_backup.py`

**Interfaces:**
- Consumes: `app.ingest.WATERMARK_FILENAME` (`.games_db_watermark`), `app.config.get_settings` (CLI only)
- Produces:
  - `class ChromaArchiveError(Exception)`
  - `ARCHIVE_ROOT = "chroma"`
  - `tarball_path(persist_dir: Path) -> Path` — `persist_dir.parent / f"{persist_dir.name}.tar.gz"`
  - `backup_chroma(persist_dir: Path, dest: Path | None = None) -> Path` — writes gzip tar, returns dest path; raises `ChromaArchiveError` if persist dir missing or watermark missing

- [ ] **Step 1: Write the failing tests**

Create `tests/test_chroma_backup.py`:

```python
from pathlib import Path

import pytest

from app.ingest import WATERMARK_FILENAME
from scripts.backup_chroma import ChromaArchiveError, backup_chroma, tarball_path


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
    import tarfile

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_chroma_backup.py -v`

Expected: FAIL with import error (`scripts.backup_chroma` not found) or `backup_chroma` not defined

- [ ] **Step 3: Write minimal implementation**

Create `scripts/backup_chroma.py`:

```python
#!/usr/bin/env python3
"""Backup live Chroma persist dir to a gzip tarball."""

from __future__ import annotations

import argparse
import logging
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
    fd, tmp_name = tempfile.mkstemp(prefix=dest.name + ".", suffix=".tmp", dir=dest.parent)
    tmp_path = Path(tmp_name)
    try:
        import os

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
    parser = argparse.ArgumentParser(description="Backup live Chroma index to a gzip tarball")
    args = parser.parse_args()
    del args
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
```

Keep `os.close` at module level: move `import os` to the top of the file with the other imports (do not leave it inside the function).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_chroma_backup.py -v`

Expected: PASS (all four tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/backup_chroma.py tests/test_chroma_backup.py
git commit -m "$(cat <<'EOF'
feat: backup live Chroma persist dir to gzip tarball

EOF
)"
```

---

### Task 2: Restore tarball over live Chroma

**Files:**
- Create: `scripts/restore_chroma.py`
- Modify: `tests/test_chroma_backup.py`

**Interfaces:**
- Consumes: `ChromaArchiveError`, `ARCHIVE_ROOT`, `tarball_path`, `backup_chroma` from `scripts.backup_chroma`; `WATERMARK_FILENAME`
- Produces: `restore_chroma(archive: Path, persist_dir: Path) -> None` — raises `ChromaArchiveError` if archive missing or `chroma/.games_db_watermark` absent; on failure live dir is unchanged; does not touch `<persist_dir.name>_staging`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chroma_backup.py`:

```python
from scripts.restore_chroma import restore_chroma


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
    import tarfile

    live = _live_dir(tmp_path, watermark="live-mark")
    archive = tmp_path / "chroma.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name="chroma/chroma.sqlite3")
        data = b"x"
        info.size = len(data)
        tar.addfile(info, fileobj=__import__("io").BytesIO(data))

    with pytest.raises(ChromaArchiveError):
        restore_chroma(archive, live)

    assert (live / WATERMARK_FILENAME).read_text() == "live-mark"
```

Use `io.BytesIO` via `import io` at the top of the test file instead of `__import__`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_chroma_backup.py::test_restore_replaces_live_and_leaves_staging tests/test_chroma_backup.py::test_restore_without_watermark_leaves_live -v`

Expected: FAIL with import error (`scripts.restore_chroma` not found)

- [ ] **Step 3: Write minimal implementation**

Create `scripts/restore_chroma.py`:

```python
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
from app.ingest import WATERMARK_FILENAME
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
    parser = argparse.ArgumentParser(description="Restore live Chroma index from gzip tarball")
    args = parser.parse_args()
    del args
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
```

If `tarfile.extractall(..., filter="data")` is unsupported on the project Python, omit `filter="data"` and keep `_safe_members` path checks. Python 3.12 supports `filter`.

`extracted.rename(persist_dir)` must work across the same filesystem (temp dir is `persist_dir.parent`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_chroma_backup.py -v`

Expected: PASS (all six tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/restore_chroma.py tests/test_chroma_backup.py
git commit -m "$(cat <<'EOF'
feat: restore live Chroma from gzip tarball

EOF
)"
```

---

### Task 3: Docs and gitignore

**Files:**
- Modify: `docs/database.md` (after the RAG ingest paragraph, before `## Backup` — insert `## Chroma backup` so Postgres stays under `## Backup`)
- Modify: `README.md` (Data / database ops sentence)
- Modify: `.gitignore`

**Interfaces:**
- Consumes: CLI `python scripts/backup_chroma.py` and `python scripts/restore_chroma.py`; `CHROMA_PERSIST_DIR`
- Produces: operator instructions only; no new code APIs

- [ ] **Step 1: Update `.gitignore`**

Append:

```
data/chroma.tar.gz
```

Keep existing `data/chroma/`.

- [ ] **Step 2: Add Chroma section to `docs/database.md`**

Insert immediately before `## Backup` (Postgres). Outer fence is four backticks so inner bash fences stay intact:

````markdown
## Chroma backup

Live index only (`CHROMA_PERSIST_DIR`, default `./data/chroma`). Staging and `chroma_old` are not included.

Stop the API first so `chroma.sqlite3` is complete (wait until ingest/swap finished). Scripts do not lock Chroma.

Canonical local path: `data/chroma.tar.gz` (overwrites that file, via a temp file).

```bash
# Stop uvicorn / compose api first
python scripts/backup_chroma.py
```

Requires live `.games_db_watermark`. If that file is missing, backup fails and the previous tarball is left in place. After a successful ingest swap, the watermark is present.

Restore (API stopped) replaces the live persist dir. `chroma_staging` is left alone.

```bash
python scripts/restore_chroma.py
```

Copy `data/chroma.tar.gz` to another machine, place it next to that host’s persist dir (`<persist_dir>.tar.gz`), restore, then start the API. Startup skips re-index only when the restored watermark equals the current eligible-game DB watermark (`count:max(updated_at)`). If the catalog has changed, ingest refreshes as usual (staging resume unchanged).
````

- [ ] **Step 3: Point README at Chroma backup**

In `README.md`, change:

```markdown
Database ops (BGG dump import, backup, restore): [docs/database.md](docs/database.md).
```

to:

```markdown
Database ops (BGG dump import, Postgres and Chroma backup/restore): [docs/database.md](docs/database.md).
```

- [ ] **Step 4: Run tests (no behavior change)**

Run: `pytest tests/test_chroma_backup.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add .gitignore docs/database.md README.md
git commit -m "$(cat <<'EOF'
docs: chroma live index backup and restore

EOF
)"
```

---

## Spec coverage

| Spec item | Task |
|-----------|------|
| Live-only backup | 1 (`tar.add` persist dir only) |
| Never staging / chroma_old | 1–2 (not in archive; restore test leaves staging) |
| `chroma/` archive root | 1 |
| `<persist_dir>.tar.gz` + atomic overwrite | 1 (`mkstemp` then `replace`) |
| Backup fails without watermark; existing tarball kept | 1 |
| Restore fails without watermark; live unchanged | 2 |
| Restore replaces live | 2 |
| Staging untouched | 2 |
| No ingest changes / no auto-restore / no process lock | all (out of those files) |
| Docs + gitignore | 3 |
