# Chroma live index backup and restore

Date: 2026-08-28  
Status: approved design (pending implementation plan)

## Goal

Copy a finished live Chroma index as a tarball so another machine or a wiped `data/` can start the API without re-embedding, as long as the DB watermark still matches.

## Decisions

| Topic | Choice |
|-------|--------|
| What is backed up | Live persist dir only (`CHROMA_PERSIST_DIR`, default `data/chroma`) |
| Staging / `chroma_old` | Never included |
| Format | gzip tarball at `<persist_dir>.tar.gz` (default `data/chroma.tar.gz`); overwrite via temp file then rename |
| Skip re-index | Existing live `.games_db_watermark` vs current DB watermark; backup does not change ingest |
| Watermark missing | Backup fails; restore of an archive without that file also fails |
| Consistency | Operator stops the API (or waits until swap finished) so sqlite is complete; scripts do not lock or kill processes |
| Auto-restore on startup | No |

## Data flow

```text
backup (API stopped, live index complete)
  → require live/.games_db_watermark
  → tar cz: chroma/  (contents of live dir, including watermark)
  → write `<persist_dir>.tar.gz` atomically

restore (API stopped)
  → require archive contains chroma/.games_db_watermark
  → replace live dir with unpacked chroma/
  → leave chroma_staging untouched

startup ingest_games (unchanged)
  → live watermark match → skip embed
  → else staging resume / re-index as today
```

## Components

### `scripts/backup_chroma.py`

Reads `CHROMA_PERSIST_DIR` from settings/env. Fails if the live dir or `.games_db_watermark` is missing. Writes `<persist_dir>.tar.gz` with a single top-level `chroma/` directory (name is always `chroma/`, not the persist folder’s basename).

### `scripts/restore_chroma.py`

Reads the same tarball path. Fails if `chroma/.games_db_watermark` is absent. Replaces the live persist dir (rmtree then unpack). Does not read or write staging.

### Docs

Add a Chroma backup/restore section next to Postgres in `docs/database.md`, with a README pointer. Document: stop uvicorn first; after restore, skip-on-start only if watermark equals the current eligible-game watermark. Gitignore `data/chroma.tar.gz`.

## Error handling

| Event | Result |
|-------|--------|
| Live missing or empty | Backup fails |
| Live has no `.games_db_watermark` | Backup fails (current live without watermark cannot skip re-index after restore) |
| Tarball missing or not a chroma archive | Restore fails; live unchanged |
| DB watermark changed after backup | Restore succeeds; next start re-indexes (or resumes staging) as today |
| API still running / sqlite mid-write | Undefined; operator must stop first |

## Testing

- Backup of a temp live dir with watermark produces a tarball whose `chroma/.games_db_watermark` matches.
- Backup without watermark fails and does not replace an existing tarball.
- Restore replaces live; a sibling `chroma_staging` dir is unchanged.
- Restore of an archive without watermark fails and does not delete live.

## Out of scope

- Staging in the archive or restore.
- Startup auto-restore.
- Process locking / detecting an open Chroma client.
- Writing a watermark onto an existing live index that lacks one.
- Changing ingest, swap, or watermark formula.
