# Similar-to neighbor ranking (mechanics in Chroma)

Date: 2026-08-20  
Status: approved design (pending implementation plan)

## Goal

Support queries like “games like Catan” by ranking Chroma neighbors of the seed game’s ingest text. Put mechanics into that text so neighbors share play patterns, not only blurb wording. Keep existing SQL hard filters.

## Decisions

| Topic | Choice |
|-------|--------|
| Similarity | Chroma neighbors of the seed document text |
| Seed query | Rebuild `_document_text` from Postgres (same string as ingest); not stored-embedding search |
| Seed lookup miss | Embed the user query as today; do not drop a game |
| Resolved seed | Drop that `game_id` from hits |
| Other filters | SQL allowlist unchanged, then neighbor rank inside those ids |
| `similar_to` extraction (this pass) | LLM prompt on `ExtractedFilters.similar_to` |
| Text extractor phrases | `docs/superpowers/specs/2026-08-20-text-filter-extraction-design.md` |
| Name lookup | Eligible/indexed games only; Postgres `pg_trgm` ≥ 0.3 top 1; SQLite `ILIKE '%q%'` |
| Catalog `/search` | Out of scope; must reuse the name-match helper later |
| Chroma metadata | Unchanged; mechanics are embed text only |
| Watermark | Unchanged `{count}:{stamp}`; operator force-reindexes for mechanics |

## Architecture

```
query
  → extract_filters (LLM) → ExtractedFilters including similar_to
  → if similar_to:
        lookup_indexed_game_by_name(session, similar_to)
        hit  → chroma_query = _document_text(seed row)
                exclude_id = seed.game_id
        miss → chroma_query = user query + keywords (today)
                exclude_id = none
  → SQL candidates from hard filters (similar_to is not a predicate)
  → Chroma neighbors of chroma_query within candidate ids
  → drop exclude_id; return top_k
```

`similar_to` is a seed name, not a hard filter. `has_active_hard_filters` / `HARD_FILTER_FIELDS` stay unchanged.

Units:

| Unit | Responsibility |
|------|----------------|
| `ExtractedFilters.similar_to` | Game name string or null |
| `extract_filters` prompt | Fill `similar_to` from like / similar-to / alternatives-to language |
| `name_match_predicate` + `lookup_indexed_game_by_name` | Dialect-aware top-1 name → eligible `Game` |
| `_document_text` / `_game_to_row` | Include mechanics in embed text |
| `retrieve_games` | Choose chroma query; drop seed id |

`/recommend` status codes unchanged. Lookup miss is not 4xx.

## Ingest

Embedded text:

```
{name}. {description}. Categories: {categories}. Mechanics: {mechanics}.
```

Mechanics mapping matches categories: join names, lowercase, spaces → underscores, comma-joined. If the game has no mechanics, omit the `Mechanics:` clause. Do not default a mechanic.

`load_games_for_rag` `selectinload`s `Game.mechanics` → `GameMechanic.mechanic` beside categories.

Chroma metadata is unchanged (`name`, player counts, play time, categories, optional complexity/weight/age/year/poll encodings, `game_id`).

Watermark format does not change. Documents without mechanics remain until a manual/forced reindex.

## Extraction (this pass)

Add `similar_to: str | None` to `ExtractedFilters` and `FiltersApplied`. Empty string normalizes to null.

LLM prompt additions:

- Set `similar_to` to the referenced game **name** when the user asks for games like / similar to / alternatives to X.
- Only set it when a name is explicit. Do not guess.
- Do not put that name in `keywords` when `similar_to` is set.
- Other fields unchanged (players, time, categories, etc. still extract when present).

Text-first phrase rules live in `docs/superpowers/specs/2026-08-20-text-filter-extraction-design.md` (`similar_to` triggers, name capture, skip LLM when `similar_to` is set on short queries).

## Name lookup

`lookup_indexed_game_by_name(session, q) -> Game | None`.

Scope: same eligibility as ingest (`_eligible_games_filters`). Expansions and uncrawled rows cannot be seeds (they have no Chroma doc).

Postgres: `name % :q` and `similarity(name, :q) >= 0.3`, order `similarity DESC, rank ASC NULLS LAST, id ASC`, limit 1.

SQLite (tests): `name ILIKE '%' || :q || '%'`, order `rank ASC NULLS LAST, id ASC`, limit 1.

Return the ORM row (for `_game_to_row`) or `None`.

Schema (also required later by catalog search):

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_games_name_trgm ON games USING gin (name gin_trgm_ops);
```

Put this in `scripts/schema.sql` and ship `scripts/migrate_pg_trgm.sql` for existing Postgres volumes (same one-shot pattern as `migrate_taxonomy_poll_summary.sql`; no Alembic). Note it in `docs/database.md`. SQLite tests skip the extension and GIN index.

Shared helper: name-match SQL (predicate + similarity/order for Postgres, ILIKE for SQLite) lives in one module (e.g. `app/name_match.py`). Catalog search must call it rather than copy the dialect split.

## Retrieval

When lookup hits:

1. `chroma_query = _document_text(_game_to_row(seed))`.
2. Existing SQL allowlist + `_similarity_within_ids`.
3. Request `top_k + 1` when `exclude_id` is set; drop documents whose `game_id` equals the seed; return at most `top_k`.

When lookup misses or `similar_to` is null: `_search_query(filters, user_query)` as today. Do not drop an id.

`filters_applied` still reports `similar_to` when the LLM set it, including lookup misses.

Relaxation stages ignore `similar_to` (nothing to drop). The seed query string stays the same across stages.

## Error handling

- Ordinary extract: missing like-language → `similar_to` null.
- Lookup miss → sentence fallback; log at info with the unmatched name.
- `pg_trgm` missing on Postgres → ingest/lookup errors surface as startup or query failure; enable the extension via schema/migrate (do not silently ILIKE in production).
- No new `/recommend` 502 for lookup miss.

## Testing

- Ingest: mechanics appear in `page_content`; games with no mechanics have no `Mechanics:` substring.
- Lookup (SQLite): substring top hit by rank; no match → `None`; ineligible row (expansion) not returned.
- Retriever: resolved seed uses document text as the Chroma query; seed `game_id` absent from results; unresolved `similar_to` uses the user query.
- LLM schema: `similar_to` round-trip on `ExtractedFilters`.
- API: `filters_applied.similar_to` set when the extractor returns it.

## Out of scope

- Catalog `POST /search` / autocomplete (reuse `name_match` when that spec is built).
- Query-by-stored-embedding.
- Copying seed mechanics/categories into SQL filters.
- Changing watermark to bust cache on document-format change.
- Min play time, player ranges, or other new filter fields besides `similar_to`.

## File touch list (implementation)

- Create: `app/name_match.py` (predicate + `lookup_indexed_game_by_name`), `tests/test_name_match.py`, `scripts/migrate_pg_trgm.sql`.
- Modify: `app/models.py`, `app/query_extractor.py`, `app/ingest.py`, `app/retriever.py`, `app/recommender.py` (`filters_to_applied`), `scripts/schema.sql`, `docs/database.md`, ingest/retriever/API tests.
- Unchanged: `HARD_FILTER_FIELDS`, watermark format, Chroma metadata shape.
