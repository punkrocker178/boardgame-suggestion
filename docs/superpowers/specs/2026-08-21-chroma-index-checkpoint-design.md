# Chroma staging resume and swap resilience

Date: 2026-08-21  
Status: approved design (pending implementation plan)

## Goal

Promote a finished staging index even when leftover `chroma_old` cannot be deleted. Resume an interrupted staging index on the next API start instead of re-embedding games already upserted.

## Decisions

| Topic | Choice |
|-------|--------|
| Checkpoint store | Staging Chroma itself (`<chroma>_staging`); no sidecar vector dump |
| Resume key | Staging `.games_db_watermark` equals current DB watermark |
| Skip unit | Document IDs already in the staging collection |
| Kill / embed failure | Keep staging; do not `rmtree` it |
| Unreadable / no staging watermark | Wipe staging and start a new index |
| `force=True` or DB watermark change | Wipe staging and start over |
| Live queries | Unchanged until swap succeeds |
| Stuck `chroma_old` | Rename live to `chroma_old.<unique>` and still promote staging |

## Data flow

```text
startup ingest_games
  → live watermark match and not force → skip (unchanged)
  → else load eligible docs
  → if staging exists:
        matching watermark → reuse collection; skip IDs already upserted
        mismatch / unreadable / missing watermark → rmtree staging
  → write current watermark into staging
  → embed + upsert remaining batches
  → swap staging → live (resilient old-dir)
  → live already has watermark after rename; rewrite is fine
```

If every ID is already in staging, skip embed and swap.

## Components

### Staging watermark

Same filename as live: `.games_db_watermark`. Written into staging before the first embed batch so a killed process can still resume.

ID for a document is unchanged: `str(game_id)` when present, else `name`.

### `_index_documents_to_dir`

Stop unconditionally wiping `target_dir`. Open or create the collection. Load existing IDs once. For each batch, drop IDs already present, then embed and upsert the rest.

Callers pass whether this run is a resume (staging kept) vs a fresh dir (caller wiped).

### `_swap_staging_to_live`

1. Clear Chroma client cache.
2. If `chroma_old` exists, try `rmtree`. On `OSError` (including `PermissionError`), leave it and pick `chroma_old.<pid>` (or timestamp if that path exists).
3. Rename live → that old path when live exists.
4. Rename staging → live. On failure, if live is gone and old exists, rename old back to live, then raise.
5. Best-effort `rmtree` of the old path we created. Failure here is a warning, not a failed ingest — live already points at the new index.
6. Clear cache.

Do not require deleting a leftover root-owned `chroma_old` in order to finish ingest.

## Error handling

| Event | Staging | Live | Result |
|-------|---------|------|--------|
| Embed / provider error | kept | unchanged | `stale=True` if live has docs |
| Ctrl-C / process kill | kept (OS) | unchanged | next start resumes if watermark matches |
| Swap live/staging rename fails | kept | restored if possible | `stale=True` if live has docs |
| Cannot delete leftover `_old` | promoted | moved to unique old name | success |
| Cannot delete unique old after swap | gone (renamed to live) | new index | success + warning |

Startup still serves live Chroma when refresh fails and live has documents.

## Testing

- Swap succeeds when deleting `_old` raises `PermissionError`.
- Partial staging + matching watermark: second `ingest_games` does not call embed for already-upserted IDs; after swap, live count equals eligible games.
- Partial staging + changed DB watermark: staging discarded; all docs embedded.
- Embed failure with existing live: staging remains (replaces the current “staging deleted” assertion).
- Existing skip-on-watermark, reindex-on-change, and keep-live-on-embed-failure behaviors stay.

## Out of scope

- Sidecar persistence of in-flight embedding API responses.
- Changing Docker user / volume ownership.
- Background indexing after the API is already serving.
- Changing batch size, retry, or watermark formula.
