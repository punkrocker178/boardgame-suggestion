# Text-first query extraction (LLM fallback)

Date: 2026-08-20  
Status: approved design (pending implementation plan)

## Goal

Extract `ExtractedFilters` from `/recommend` queries with a deterministic text extractor. Call the existing LLM extractor only as fallback. Prefer speed, testability, and surviving LLM outages during extraction.

## Decisions

| Topic | Choice |
|-------|--------|
| Primary extractor | Rule-based regex + word lists (`app/text_extractor.py`) |
| LLM extractor | Existing `extract_filters` in `query_extractor.py`; unchanged prompt/schema |
| When LLM runs | No active hard filters from text **or** sentence count > 3 |
| Combine results | LLM **replaces** the text result (no field merge) |
| LLM failure / `llm is None` | Keep text filters; no 502 at extraction |
| Hard filters | Reuse `has_active_hard_filters` (keywords do not count) |
| Category mapping | Unchanged: `apply_category_normalization` in retrieval |
| NLP libraries | None (no spaCy) |

## Architecture

`/recommend` calls `resolve_filters(llm, query)` then retrieval and synthesis as today.

```
query
  → extract_filters_from_text(query) → text_filters
  → if should_use_llm(query, text_filters) and llm is not None:
        try extract_filters(llm, query) → return llm_filters
        except provider/parse error → log warning; return text_filters
  → else return text_filters
```

`should_use_llm` is true when:

1. `not has_active_hard_filters(text_filters)`, or
2. `sentence_count(query) > 3`

Units:

| Unit | Responsibility |
|------|----------------|
| `sentence_count(query) -> int` | Split on `. ? !`; strip; drop empty chunks |
| `extract_filters_from_text(query) -> ExtractedFilters` | Regex/word lists only |
| `extract_filters(llm, query)` | Existing LLM path |
| `resolve_filters(llm: BaseChatModel \| None, query) -> ExtractedFilters` | Orchestration |
| `has_active_hard_filters` | Unchanged in `sql_filters.py` |

`main.py` must not 502 when `llm is None` during extraction. Synthesis still 502s if the LLM is unreachable.

## Sentence count

- Split with a regex on `.`, `?`, and `!`.
- Trim whitespace; ignore empty parts (so `"Hi.  "` is one sentence).
- `<= 3` sentences: length fallback does **not** fire.
- `> 3` sentences: LLM fallback fires even if text found hard filters.

Examples: `"a. b. c"` → 3; `"a. b. c. d"` → 4; `"Really? Yes!"` → 2.

## Text extractor rules

Same `ExtractedFilters` fields. Set a field only on an explicit match. Do not guess.

Matching is case-insensitive. Prefer word boundaries so `"thinking"` does not fire inside unrelated tokens, and `"hard"` does not fire inside `"hardly"`.

### Players

- `player_count`: `"for 4 players"`, `"4-player"`, `"4 players"`, word numbers one–ten. This is the default for a bare player count.
- `best_with_player_count`: `"best with 4"`, `"best at 4"`. Do not set this from a plain player count.
- `recommended_with_player_count`: `"recommended for 3"`, `"recommended with 3"`. Otherwise player language is `player_count` only.

### Play time

`max_play_time_minutes` from `"under 45 minutes"`, `"max 60 min"`, `"≤ 90 minutes"`, `"under an hour"` → 60. No min play time field exists; do not invent one.

### Complexity vs weight

Qualitative bucket words set `complexity` and leave `min_weight` / `max_weight` null unless a numeric weight phrase is also present.

Numeric BGG weight language (`weight > 3`, `at least 3.5`, `heavier than 2`, `weight under 2`) sets `min_weight` and/or `max_weight` and leaves `complexity` null unless a bucket word is also present.

If both a bucket word and a numeric weight appear, set both (same as the LLM prompt: complexity from words, weights from numbers). SQL already ORs them.

**Light:** light, easy, simple, beginner, casual, light-hearted, lighthearted, light-hearted spelling `light hearted`, gateway, filler.

**Medium:** medium, moderate, mediocre, average, mid-weight, midweight, middleweight.

**Heavy:** heavy, hard, complex, difficult, thinking, thinky, hardcore, hard core, hard-core, brain-burner, brain burner, crunchy.

Do not map category-ish words (`party`, `family`, `strategy`) to complexity.

### Age

- `min_age`: `"14+"`, `"ages 14+"`, `"adults"` → 18.
- `max_age`: `"for my 8-year-old"`, `"for kids 8"`, `"8 year old"`.
- `"8+"` is `min_age`, not `max_age`.

### Year

- `min_year`: `"after 2020"`, `"since 2018"`, `"from 2015"`.
- `max_year`: `"before 2010"`.

### Categories

A small alias map to BGG-style labels (e.g. strategy, party, card game, cooperative/coop). Unknown phrases are not written to `categories`. Retrieval still runs `apply_category_normalization`; leftovers become keywords there.

### Keywords

After matched spans are removed, leftover content words become `keywords`. Drop a small English stopword list. The raw query is still passed to Chroma; keywords are a boost only.

## Error handling

- Text extraction does not raise on ordinary strings. Empty or non-matching query → all-null `ExtractedFilters`.
- LLM fallback catches `APIConnectionError`, `APIStatusError`, and structured-output/parse failures. Log a warning with exception type. Return text filters.
- If fallback is indicated but `llm is None`, skip the LLM and return text filters.
- `/recommend` synthesis errors stay 502 `LLM unavailable`.

## Observability

Log text filters, whether fallback was attempted, `extraction_source=text|llm`, and fallback failures.

## Testing

- Table-driven unit tests per field, including complexity synonyms and the `"8+"` vs `"8-year-old"` split.
- `sentence_count` for 1, 3, 4 sentences and `?` / `!`.
- `resolve_filters`:
  1. Short query with hard filters → LLM not called.
  2. No hard filters → LLM called; LLM result used.
  3. More than 3 sentences → LLM called even if text found filters; LLM result used.
  4. LLM raises → text filters kept.
- API: `/recommend` extraction no longer requires a live LLM when text extraction succeeds and fallback is not indicated. Synthesis remains mocked/LLM-backed as today.

## Out of scope

- Merging text and LLM field-by-field.
- Changing SQL relaxation, retrieval, or synthesis prompts.
- spaCy or other NLP runtimes.
- Min play time, player ranges (`3-5 players` as a range), or new `ExtractedFilters` fields.

## File touch list (implementation)

- Create: `app/text_extractor.py`, `tests/test_text_extractor.py`, `tests/test_resolve_filters.py` (or a single test module covering both).
- Modify: `app/query_extractor.py` (`resolve_filters` + `should_use_llm`), `app/main.py` (call `resolve_filters`; drop extraction 502 when LLM is down).
- Unchanged: `app/sql_filters.py` `HARD_FILTER_FIELDS` / `has_active_hard_filters`, `app/llm_parsing.py`, category normalization.
