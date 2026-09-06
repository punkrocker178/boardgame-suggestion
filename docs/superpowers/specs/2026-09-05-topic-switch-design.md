# Topic-switch detection (phase 2)

Date: 2026-09-05  
Updated: 2026-09-06 (tightened against phase 1 code)  
Status: approved design (pending implementation plan)

Depends on: `docs/superpowers/specs/2026-09-04-multi-turn-rag-design.md` (phase 1 implemented).

## Goal

Stop follow-up rewriting from inheriting constraints when the user starts a **new** recommendation topic in the same conversation. Backend only.

## Decisions

| Topic | Choice |
|-------|--------|
| Detection | Hybrid: cheap follow-up cues inherit; otherwise one LLM call with `topic_changed` |
| On switch rewrite | Discard model rewrite; `standalone_query =` raw `query` |
| History after switch | Persist `conversations.topic_started_at`; recent load + summary refresh are epoch-scoped |
| Summary on switch | `summary = NULL` in the same `append_turn` flush as the new epoch |
| LLM shape (no cue) | `{ topic_changed, standalone_query }` in one call |
| Cue-hit LLM | Rewrite only (`standalone_query`); `topic_changed` false |
| Filters | Still `resolve_filters(standalone_query)` |
| Pipeline | Unchanged after the standalone string exists |
| Schema tooling | `scripts/schema.sql` + one-shot migrate; no Alembic |
| API | `RecommendResponse.topic_changed: bool` |
| Failed turns | Do not persist; do not move epoch or clear summary |
| Summary refresh | Rolling (same as phase 1): each non-switch turn with epoch turns `> 5` folds the dropped pair into `summary` |

## Out of scope

Clarification path, multi-intent / query decomposition, hybrid lexical retrieval, reranking, grounded citations / `MessageSources`, semantic long-term memory, frontend, real auth, evaluation harness, embedding-based topic similarity.

## Architecture

Phase 1 already lives in `app/services/contextualizer.py`, `app/services/conversation_store.py`, `app/api/routes.py`, `app/api/models.py`, `app/db/models.py`. Extract / retrieve / synthesize stay consumers of a string.

```
POST /recommend { conversation_id, query }
  → load Conversation (404 if missing)
  → epoch = conv.topic_started_at  (NULL = all history)
  → load summary + last 5 turns with created_at >= epoch
  → plan = contextualize_query(query, summary, recent)
       empty recent → QueryPlan(query, topic_changed=false), no LLM
       follow-up cue on current query → rewrite LLM (phase 1), topic_changed=false
       no cue → LLM { topic_changed, standalone_query }
            if topic_changed → standalone_query = query (ignore model rewrite)
  → resolve_filters → retrieve_games → synthesize_recommendations
  → append_turn(..., topic_changed=plan.topic_changed)
       if true: summary = NULL, topic_started_at = this user message created_at
  → else: rolling summary refresh using epoch-only turn counts
       use the epoch loaded before append_turn (not the new timestamp)
```

### Units

| Unit | Change |
|------|--------|
| `Conversation` ORM / `scripts/schema.sql` | `topic_started_at TIMESTAMPTZ NULL` |
| `load_recent_messages` / `count_turns` / `load_turn_pair_at_index` | Extra `topic_started_at: datetime \| None = None`. When set, only messages with `created_at >=` that value. `None` = current behavior. |
| `append_turn` | Extra `topic_changed: bool = False`. If true, same flush: `summary = None`, `topic_started_at =` the user row’s `created_at` (`now` already assigned to that pair). Still returns `None`. |
| `set_summary` | Unchanged (`str` only). Switch clears via `append_turn`. |
| `contextualize_query` | Return `QueryPlan(standalone_query: str, topic_changed: bool)` instead of `str`. Empty `recent_messages` → `(query, False)`, no LLM. |
| `has_followup_cue(query) -> bool` | New, same file as contextualizer. |
| `app/api/routes.py` | Pass `conv.topic_started_at` into store reads; LLM 502 only when epoch recent is non-empty; `append_turn(..., topic_changed=plan.topic_changed)`; response + payload include `topic_changed`. |
| `RecommendResponse` | Add `topic_changed: bool` |
| Extract / retrieve / synthesize | Unchanged |

`QueryPlan` is a small type in `app/services/contextualizer.py` (dataclass or NamedTuple). No new module.

## Data model

### `conversations.topic_started_at`

| Column | Type | Notes |
|--------|------|-------|
| `topic_started_at` | TIMESTAMPTZ NULL | Start of the current topic epoch. NULL = beginning of the conversation (phase 1 chats). |

Set to the **user** message `created_at` of the switching turn, using the same timestamp `append_turn` already assigns to that pair.

Do not store a message UUID as the epoch pointer (UUIDs are not time-ordered).

Ship `scripts/migrate_topic_started_at.sql` for existing volumes:

```sql
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS topic_started_at TIMESTAMPTZ;
```

Fresh installs get the column from `scripts/schema.sql`. Document operator steps in `docs/database.md` next to the conversations migrate section.

## Query planning

`contextualize_query` input is unchanged: `query`, `summary`, `recent_messages` (already epoch-filtered by the route).

### First turn in epoch

Empty `recent_messages`: return `QueryPlan(standalone_query=query, topic_changed=False)`. Do not call the LLM. Do not run the cue helper.

### Follow-up cues

Run on the **current** user `query` only (not history). Case-insensitive.

Any one hit → cue path (rewrite LLM, `topic_changed` false):

**Phrases (substring):**

- `what about`, `how about`, `same but`, `same as`, `but with`, `but for`
- `except`, `without the`, `instead of` (phrase only; lone `instead` is not a cue)

**Comparatives (substring):**

- `lighter`, `heavier`, `shorter`, `longer`, `simpler`, `cheaper`
- `more`, `less`, `another`, `other`, `similar`
- `quicker`, `easier`, `harder`, `faster`, `slower`, `bigger`, `smaller`

**Word-boundary tokens:**

- `it`, `that`, `those`, `them`, `also`, `they`, `those ones`

**Not cues:** lone `this`, lone `instead` (too many new-topic false positives, e.g. `this weekend war games`).

Cue path: do not ask the model for `topic_changed`. Call the existing rewrite prompt / `StandaloneQuery`.

### No cue (and history in epoch)

One structured call:

```json
{ "topic_changed": false, "standalone_query": "..." }
```

Prompt rules:

1. Set `topic_changed` true when the current message is a new recommendation request that must **not** keep prior players, time, complexity/weight, similar-to, or categories.
2. Otherwise `topic_changed` false: rewrite with phase 1 inherit-unless-replaced rules.
3. Do not invent game names or filters absent from the conversation.
4. Return JSON only with those two fields.

If `topic_changed` is true, **discard** `standalone_query` and use the raw `query`. A sloppy rewrite must not smuggle old constraints.

## Summary refresh

Rolling, same as phase 1, scoped to the epoch.

- Recent window: last **5** turns **inside the current epoch**.
- `count_turns` and `load_turn_pair_at_index` ignore messages with `created_at < topic_started_at` when that argument is set.
- After persist, those helpers use the **epoch loaded before `append_turn`**, not a newly written `topic_started_at`.
- Switch turn: `append_turn` clears summary and sets epoch; epoch turn count is 1; do not summarize.
- Non-switch and epoch turns `> 5`: call `summarize_dropped_turn` with prior summary + the pair dropping out of the epoch window; `set_summary` with the result. Repeat every such turn (turn 6 folds turn 1, turn 7 folds turn 2, …).
- Right after a switch the summary is NULL, so the first refresh on the new topic starts from that dropped new-topic pair only.
- Summarizer failure: log, keep current summary, still return the recommend response (phase 1).

## API

`POST /recommend` response adds:

```json
{
  "conversation_id": "<uuid>",
  "standalone_query": "...",
  "topic_changed": false
}
```

`topic_changed` is true only when the no-cue LLM path set it true and the turn persisted.

Assistant `payload` includes `topic_changed` next to existing fields (`reasoning`, `recommendations`, `filters_applied`, `filters_relaxed`, `standalone_query`).

LLM-unavailable 502 for contextualization only when epoch `recent` is non-empty (same gate as today, epoch-scoped). First turn in an epoch does not require an LLM for planning.

### Errors

Unchanged from phase 1: 422 invalid body, 404 unknown `conversation_id`, 502 contextualizer / synthesis LLM failure. Epoch and summary are not updated on those failures.

## Error handling and persistence

- Validate conversation exists before planning.
- Persist user + assistant only after successful synthesis.
- Apply `summary = NULL` and `topic_started_at = user.created_at` inside `append_turn` when `topic_changed`.
- Concurrent `/recommend` on one conversation: same as phase 1 (no conflict API).

## Testing

Pytest, SQLite, mocked LLM, existing FastAPI style:

1. Cue hit (`something lighter`, `also 2-player`): rewrite invoked; `topic_changed` false; `topic_started_at` and `summary` unchanged.
2. No cue + mock `topic_changed=true`: retrieve/synthesize receive raw `query`; response `topic_changed` true; `summary` is NULL; `topic_started_at` equals the new user message `created_at`; next `load_recent_messages` omits earlier turns.
3. No cue + mock `topic_changed=false`: `standalone_query` from the model is used; epoch unchanged.
4. First turn (empty epoch): contextualizer LLM not called; `topic_changed` false.
5. After a switch, six new-topic turns: summarizer invoked; dropped pair is from the **new** epoch only.
6. Cue helper: `also` and `instead of` match; `this weekend war games` and lone `instead` do not; `more players` matches.

Success criterion: prior “party games for 8” then “2-player war games” (no cue) with mocked `topic_changed=true` does not pass “8” / “party” into retrieve/synthesize.

## Success criteria

- New-topic prompts in an existing conversation do not inherit the previous search constraints.
- Anaphoric / comparative follow-ups (`something lighter`, `also …`, `instead of …`) still rewrite with history.
- Old messages remain in the transcript; only the rewrite window and summary epoch change.
- No frontend or auth required to verify via API tests / curl.

## Relationship to other docs

- Phase 1: `docs/superpowers/specs/2026-09-04-multi-turn-rag-design.md`
- Guide: `docs/rag-improvement-guide.md` implementation-order step 4 (topic-switch); body section 8. Clarification and later steps stay separate specs.
