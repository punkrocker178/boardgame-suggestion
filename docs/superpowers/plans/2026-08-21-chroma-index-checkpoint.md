# Chroma staging resume and swap resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote staging even when leftover `chroma_old` cannot be deleted, and resume an interrupted staging index on the next ingest instead of re-embedding upserted IDs.

**Architecture:** Staging Chroma is the checkpoint. A matching `.games_db_watermark` in `chroma_staging` means reuse the collection and skip existing IDs. Swap tries to delete `chroma_old`; on `OSError` it moves live to `chroma_old.<pid>` (or a timestamped name) and still promotes staging. Failed ingest no longer deletes staging.

**Tech Stack:** Existing `app/ingest.py` Chroma + LangChain path, pytest + in-memory SQLite `db_session`, `langchain_core.embeddings.FakeEmbeddings`

**Spec:** `docs/superpowers/specs/2026-08-21-chroma-index-checkpoint-design.md`

## Global Constraints

- Checkpoint store is staging Chroma (`<chroma_dir.name>_staging`); no sidecar vector dump
- Resume when staging `.games_db_watermark` equals the current DB watermark
- Skip unit is document IDs already in the staging collection (`str(game_id)` or `name`)
- Do not `rmtree` staging on embed failure, Ctrl-C, or swap failure
- Unreadable staging, missing staging watermark, `force=True`, or DB watermark change → wipe staging and start over
- Live Chroma stays the query source until swap succeeds
- Stuck `chroma_old`: rename live to a unique `_old` name and still promote staging
- Post-swap `rmtree` of old is best-effort; failure is a warning, ingest succeeds
- Do not change batch size, retry policy, or watermark formula `{count}:{stamp}`
- Out of scope: Docker user/ownership, background indexing after serve, persisting in-flight embed API bytes

---

## File map

| File | Responsibility |
|------|----------------|
| `app/ingest.py` | Unique old-dir swap; keep staging on failure; resume skip-IDs |
| `tests/test_ingest.py` | Swap, keep-staging, resume, watermark-wipe, force-wipe tests |

No new modules. Helpers stay in `app/ingest.py`.

---

### Task 1: Resilient `_swap_staging_to_live`

**Files:**
- Modify: `app/ingest.py` (`_old_dir`, `_swap_staging_to_live`; add `_try_rmtree` / unique-old helper)
- Test: `tests/test_ingest.py`

**Interfaces:**
- Consumes: `Path`, `shutil.rmtree`, `os.getpid`, existing `_old_dir(live: Path) -> Path` (`live.with_name(live.name + "_old")`)
- Produces:
  - `_try_rmtree(path: Path) -> bool` — `True` if removed or missing after attempt; `False` on `OSError`
  - `_unique_old_dir(live: Path) -> Path` — `chroma_old` if free, else `chroma_old.{pid}`, else `chroma_old.{pid}.{time.time_ns()}`
  - `_swap_staging_to_live(staging: Path, live: Path) -> None` — never raises solely because leftover `_old` could not be deleted; after successful staging→live rename, leftover old `rmtree` failure is logged, not raised

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_ingest.py`:

```python
import shutil

from app.ingest import _swap_staging_to_live


def test_swap_succeeds_when_leftover_old_cannot_be_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "chroma"
    staging = tmp_path / "chroma_staging"
    old = tmp_path / "chroma_old"
    live.mkdir()
    (live / "live.txt").write_text("live")
    staging.mkdir()
    (staging / "new.txt").write_text("new")
    old.mkdir()
    (old / "stuck.txt").write_text("stuck")

    def rmtree(path, *args, **kwargs):
        target = Path(path)
        if target.resolve() == old.resolve() or target.name == "chroma_old":
            raise PermissionError(13, "Permission denied", str(target))
        shutil.rmtree(path, *args, **kwargs)

    monkeypatch.setattr("app.ingest.shutil.rmtree", rmtree)

    _swap_staging_to_live(staging, live)

    assert (live / "new.txt").read_text() == "new"
    assert not staging.exists()
    assert old.exists()
    assert (old / "stuck.txt").exists()
    unique_olds = [
        p for p in tmp_path.iterdir() if p.name.startswith("chroma_old.")
    ]
    assert unique_olds
    assert (unique_olds[0] / "live.txt").read_text() == "live"


def test_swap_succeeds_when_post_swap_rmtree_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "chroma"
    staging = tmp_path / "chroma_staging"
    live.mkdir()
    (live / "live.txt").write_text("live")
    staging.mkdir()
    (staging / "new.txt").write_text("new")

    def boom(path, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr("app.ingest.shutil.rmtree", boom)

    _swap_staging_to_live(staging, live)

    assert (live / "new.txt").read_text() == "new"
    assert (tmp_path / "chroma_old" / "live.txt").read_text() == "live"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest.py::test_swap_succeeds_when_leftover_old_cannot_be_deleted tests/test_ingest.py::test_swap_succeeds_when_post_swap_rmtree_fails -v`

Expected: FAIL (`PermissionError` from current `_swap_staging_to_live` `shutil.rmtree(old)` with no fallback / swallow)

- [ ] **Step 3: Write minimal implementation**

Add `import os` next to the other stdlib imports in `app/ingest.py`.

Replace `_old_dir` / `_swap_staging_to_live` with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ingest.py::test_swap_succeeds_when_leftover_old_cannot_be_deleted tests/test_ingest.py::test_swap_succeeds_when_post_swap_rmtree_fails tests/test_ingest.py -v`

Expected: PASS (full ingest file still green)

- [ ] **Step 5: Commit**

```bash
git add app/ingest.py tests/test_ingest.py
git commit -m "$(cat <<'EOF'
fix: promote Chroma staging when leftover chroma_old is undeletable

EOF
)"
```

---

### Task 2: Keep staging when ingest fails

**Files:**
- Modify: `app/ingest.py` (`ingest_games` `except` block ~404–407)
- Modify: `tests/test_ingest.py` (`test_ingest_keeps_live_on_embed_failure`)

**Interfaces:**
- Consumes: `ingest_games`, `_staging_dir`, existing stale fallback
- Produces: on exception, staging directory is left in place (`ignore_errors` delete removed); live unchanged; `IngestResult(..., stale=True)` when live has docs (unchanged)

- [ ] **Step 1: Change the failing assertion**

In `test_ingest_keeps_live_on_embed_failure`, replace:

```python
    assert not (tmp_path / "chroma_staging").exists()
```

with:

```python
    assert (tmp_path / "chroma_staging").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ingest.py::test_ingest_keeps_live_on_embed_failure -v`

Expected: FAIL (`AssertionError` — staging currently deleted)

- [ ] **Step 3: Write minimal implementation**

In `ingest_games`, delete the staging cleanup in `except`:

```python
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
```

Do not `shutil.rmtree(staging)` here.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ingest.py::test_ingest_keeps_live_on_embed_failure tests/test_ingest.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/ingest.py tests/test_ingest.py
git commit -m "$(cat <<'EOF'
fix: keep Chroma staging after a failed re-index

EOF
)"
```

---

### Task 3: Resume staging by skipping existing IDs

**Files:**
- Modify: `app/ingest.py` (`_index_documents_to_dir`, `ingest_games`; add `_document_id`, `_staging_is_resumable`, `_collection_ids`, `_prepare_staging`)
- Modify: `tests/test_ingest.py`

**Interfaces:**
- Consumes: `documents: list[Document]`, `target_dir: Path`, `embeddings: Embeddings`, `batch_size: int`, `max_retries: int`, `resume: bool`
- Produces:
  - `_document_id(doc: Document) -> str` — `str(doc.metadata["game_id"])` if `"game_id" in metadata` else `doc.metadata["name"]`
  - `_staging_is_resumable(staging: Path, current_watermark: str) -> bool` — `False` if missing dir, missing watermark file, `OSError` reading it, or text ≠ `current_watermark`
  - `_prepare_staging(staging: Path, current_watermark: str, *, force: bool) -> bool` — wipes staging when `force` or not resumable; returns `True` iff staging still exists after that (resume)
  - `_collection_ids(store: Chroma) -> set[str]` — IDs from `store._collection.get()`; empty set on error
  - `_index_documents_to_dir(..., *, resume: bool = False) -> None` — if not `resume` and dir exists, `rmtree`; load existing IDs when `resume`; skip those IDs; embed+upsert the rest
  - `ingest_games`: after deciding not to skip live watermark, `_prepare_staging`; write current watermark into staging before batches; pass `resume=` into `_index_documents_to_dir`

- [ ] **Step 1: Write the failing tests**

Add helpers (next to `BoomEmbeddings`) and tests:

```python
class CountingEmbeddings(FakeEmbeddings):
    def __init__(self, size: int = 8) -> None:
        super().__init__(size=size)
        self.embedded_texts: list[str] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.embedded_texts.extend(texts)
        return super().embed_documents(texts)


class BoomAfterEmbeddings(FakeEmbeddings):
    def __init__(self, size: int = 8, *, succeed_calls: int = 1) -> None:
        super().__init__(size=size)
        self.succeed_calls = succeed_calls
        self.calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls > self.succeed_calls:
            raise TimeoutError("embedding unavailable")
        return super().embed_documents(texts)


def _seed_second_eligible(session: Session) -> Game:
    game = Game(
        id=2,
        name="Catan",
        rank=2,
        is_expansion=False,
        crawl_status=CrawlStatus.COMPLETED,
        description="Trade and build.",
        min_players=3,
        max_players=4,
        playing_time=90,
        crawled_at=datetime.now(UTC),
        updated_at=datetime(2026, 8, 2, tzinfo=UTC),
    )
    game.categories.append(GameCategory(category_id=1021))
    session.add(game)
    session.commit()
    return game
```

`_seed_second_eligible` must run after `_seed_eligible` so category `1021` exists.

```python
def test_ingest_resumes_staging_without_reembedding_upserted_ids(
    db_session: Session, tmp_path: Path
) -> None:
    _seed_eligible(db_session)
    _seed_second_eligible(db_session)
    chroma_dir = tmp_path / "chroma"
    first = ingest_games(
        db_session,
        chroma_dir,
        BoomAfterEmbeddings(size=8, succeed_calls=1),
        batch_size=1,
        max_retries=0,
    )
    assert first.stale is False
    staging = tmp_path / "chroma_staging"
    assert staging.exists()
    assert (staging / ".games_db_watermark").exists()

    counter = CountingEmbeddings(size=8)
    second = ingest_games(
        db_session, chroma_dir, counter, batch_size=1, max_retries=0
    )
    assert second.stale is False
    assert second.indexed_count == 2
    assert count_indexed_games(chroma_dir, FakeEmbeddings(size=8)) == 2
    assert len(counter.embedded_texts) == 1
    assert not staging.exists()


def test_ingest_discards_staging_when_watermark_changes(
    db_session: Session, tmp_path: Path
) -> None:
    game = _seed_eligible(db_session)
    _seed_second_eligible(db_session)
    chroma_dir = tmp_path / "chroma"
    ingest_games(
        db_session,
        chroma_dir,
        BoomAfterEmbeddings(size=8, succeed_calls=1),
        batch_size=1,
        max_retries=0,
    )
    game.name = "Brass: Birmingham (Revised)"
    game.updated_at = datetime(2026, 8, 12, tzinfo=UTC)
    db_session.commit()

    counter = CountingEmbeddings(size=8)
    result = ingest_games(
        db_session, chroma_dir, counter, batch_size=1, max_retries=0
    )
    assert result.stale is False
    assert result.indexed_count == 2
    assert count_indexed_games(chroma_dir, FakeEmbeddings(size=8)) == 2
    assert len(counter.embedded_texts) == 2


def test_ingest_force_wipes_staging(
    db_session: Session, tmp_path: Path
) -> None:
    _seed_eligible(db_session)
    _seed_second_eligible(db_session)
    chroma_dir = tmp_path / "chroma"
    ingest_games(
        db_session,
        chroma_dir,
        BoomAfterEmbeddings(size=8, succeed_calls=1),
        batch_size=1,
        max_retries=0,
    )
    counter = CountingEmbeddings(size=8)
    result = ingest_games(
        db_session,
        chroma_dir,
        counter,
        force=True,
        batch_size=1,
        max_retries=0,
    )
    assert result.skipped is False
    assert result.indexed_count == 2
    assert len(counter.embedded_texts) == 2
```

Note: first boom ingest has **no live index yet**, so `ingest_games` currently **raises** `TimeoutError` (stale fallback only when live_count > 0). The resume test must catch that:

```python
    with pytest.raises(TimeoutError):
        ingest_games(
            db_session,
            chroma_dir,
            BoomAfterEmbeddings(size=8, succeed_calls=1),
            batch_size=1,
            max_retries=0,
        )
```

Use `pytest.raises(TimeoutError)` for the first call in all three tests above (not `assert first.stale is False`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest.py::test_ingest_resumes_staging_without_reembedding_upserted_ids tests/test_ingest.py::test_ingest_discards_staging_when_watermark_changes tests/test_ingest.py::test_ingest_force_wipes_staging -v`

Expected: FAIL — `_index_documents_to_dir` still wipes staging, so resume re-embeds both (or staging watermark missing / resume path absent)

- [ ] **Step 3: Write minimal implementation**

Add helpers and change index + ingest. Document ID helper used by the batch loop:

```python
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
```

Replace `_index_documents_to_dir` with:

```python
def _index_documents_to_dir(
    documents: list[Document],
    target_dir: Path,
    embeddings: Embeddings,
    *,
    batch_size: int,
    max_retries: int,
    resume: bool = False,
) -> None:
    _clear_chroma_client_cache()
    if target_dir.exists() and not resume:
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    store = Chroma(
        collection_name=COLLECTION_NAME,
        persist_directory=str(target_dir),
        embedding_function=embeddings,
    )
    try:
        existing = _collection_ids(store) if resume else set()
        pending = [doc for doc in documents if _document_id(doc) not in existing]
        for batch in _chunks(pending, batch_size):
            texts = [doc.page_content for doc in batch]
            metadatas = [doc.metadata for doc in batch]
            ids = [_document_id(doc) for doc in batch]
            vectors = embed_documents_with_retry(
                embeddings, texts, max_retries=max_retries
            )
            store._collection.upsert(
                ids=ids,
                documents=texts,
                metadatas=metadatas,
                embeddings=vectors,
            )
    finally:
        _clear_chroma_client_cache()
```

In `ingest_games`, after `staging = _staging_dir(chroma_dir)`:

```python
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
            resume=resume,
        )
        _swap_staging_to_live(staging, chroma_dir)
        watermark_file.write_text(current_watermark)
```

Keep the Task 2 `except` (no staging delete).

If `pending` is empty, the `for batch` loop does nothing and swap still runs.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ingest.py -v`

Expected: PASS, including resume / watermark-change / force / keep-live-on-embed-failure / swap tests

- [ ] **Step 5: Commit**

```bash
git add app/ingest.py tests/test_ingest.py
git commit -m "$(cat <<'EOF'
feat: resume interrupted Chroma staging by skipping upserted IDs

EOF
)"
```

---

## Spec coverage

| Spec item | Task |
|-----------|------|
| Unique `_old` when leftover cannot be deleted | 1 |
| Post-swap `rmtree` failure is warning, ingest succeeds | 1 |
| Keep staging on embed failure | 2 |
| Staging watermark before batches | 3 |
| Skip existing IDs / empty pending → swap | 3 |
| Wipe on watermark mismatch | 3 |
| Wipe on `force=True` | 3 |
| Wipe on missing/unreadable staging watermark (`_staging_is_resumable` → False) | 3 |
| Live unchanged until swap | 1–3 (existing) |
| No sidecar dump / no Docker / no watermark formula change | out of scope |
