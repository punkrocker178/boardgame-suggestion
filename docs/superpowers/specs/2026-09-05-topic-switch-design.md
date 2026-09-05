# Topic-switch detection (phase 2)

Date: 2026-09-05  
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
| Summary on switch | Set `summary` to NULL after successful persist |
| LLM shape (no cue) | `{ topic_changed, standalone_query }` in one call |
| Cue-hit LLM | Rewrite only (`standalone_query`), same inherit-unless-replaced rules as phase 1 |
| Filters | Still `resolve_filters(standalone_query)` |
| Pipeline | Unchanged after the standalone string exists |
| Schema tooling | `scripts/schema.sql` + one-shot migrate; no Alembic |
| API | `RecommendResponse.topic_changed: bool` |
| Failed turns | Do not persist; do not move epoch or clear summary |

## Out of scope

Clarification path, multi-intent / query decomposition, hybrid lexical retrieval, reranking, grounded citations / `MessageSources`, semantic long-term memory, frontend, real auth, evaluation harness, embedding-based topic similarity.

## Architecture

```
POST /recommend { conversation_id, query }
  → load Conversation (404 if missing)
  → load summary + last 5 turns with created_at >= topic_started_at (NULL epoch = all history)
  → plan:
       no messages in epoch → standalone_query = query, topic_changed = false
       follow-up cue on current query → rewrite LLM (phase 1 rules), topic_changed = false
       no cue → LLM { topic_changed, standalone_query }
            if topic_changed → standalone_query = query (ignore model rewrite)
  → resolve_filters → retrieve_games → synthesize_recommendations
  → append user + assistant Messages
  → if topic_changed: summary = NULL, topic_started_at = this user message created_at
  → else: best-effort summary refresh using epoch-only turn counts
```

Units:

| Unit | Change |
|------|--------|
| `Conversation` ORM / schema | `topic_started_at TIMESTAMPTZ NULL` |
| `app/services/conversation_store.py` | Filter recent/count/dropped-turn by epoch; set epoch + clear summary on switch |
| `app/services/contextualizer.py` | Cue helper; rewrite vs plan structured outputs; return `standalone_query` + `topic_changed` |
| `app/api/routes.py` | Wire plan result; persist epoch side effects after success |
| `app/api/models.py` | `RecommendResponse.topic_changed` |
| Extract / retrieve / synthesize | Unchanged |

## Data model

### `conversations.topic_started_at`

| Column | Type | Notes |
|--------|------|-------|
| `topic_started_at` | TIMESTAMPTZ NULL | Start of the current topic epoch. NULL = beginning of the conversation (phase 1 chats). |

Set to the **user** message `created_at` of the switching turn, using the same timestamp `append_turn` already assigns to that pair.

Do not store a message UUID as the epoch pointer (UUIDs are not time-ordered).

Ship `scripts/migrate_topic_started_at.sql` for existing volumes (`ALTER TABLE conversations ADD COLUMN IF NOT EXISTS topic_started_at TIMESTAMPTZ`). Fresh installs get the column from `scripts/schema.sql`. Document operator steps in `docs/database.md`.

## Query planning

### Follow-up cues

Run on the **current** user `query` only (not history). Case-insensitive.

Phrase / token hits (any one → cue path):

- `what about`, `how about`, `same but`
- `lighter`, `heavier`, `shorter`, `longer`, `simpler`, `cheaper`
- word-boundary: `it`, `that`, `those`, `them`, `also`

Not cues: lone `this`, `instead` (too many new-topic false positives).

Cue path: do not ask the model for `topic_changed`. Call the existing rewrite prompt; `topic_changed` is false.

### First turn in epoch

No prior messages with `created_at >= topic_started_at` (or no messages when epoch is NULL): do not call the LLM; `standalone_query = query`; `topic_changed = false`.

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

- Recent window: last **5** turns **inside the current epoch**.
- `count_turns` and `load_turn_pair_at_index` for summarization must ignore messages with `created_at < topic_started_at` when the column is set.
- After a switch, epoch turn count is 1: do not summarize.
- After enough **new-topic** turns, summarizer input is prior (possibly NULL) summary + the pair dropping out of the epoch window. Right after a switch the summary is NULL, so the new topic’s summary starts clean.
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

### Errors

Unchanged from phase 1: 422 invalid body, 404 unknown `conversation_id`, 502 contextualizer / synthesis LLM failure. Epoch and summary are not updated on those failures.

## Error handling and persistence

- Validate conversation exists before planning.
- Persist user + assistant only after successful synthesis.
- Apply `summary = NULL` and `topic_started_at = user.created_at` in the same transaction as `append_turn` when `topic_changed`.
- Concurrent `/recommend` on one conversation: same as phase 1 (no conflict API).

## Testing

Pytest, SQLite, mocked LLM, existing FastAPI style:

1. Cue hit (`something lighter`, `also 2-player`): rewrite invoked; `topic_changed` false; `topic_started_at` and `summary` unchanged.
2. No cue + mock `topic_changed=true`: retrieve/synthesize receive raw `query`; response `topic_changed` true; `summary` is NULL; `topic_started_at` equals the new user message `created_at`; next `load_recent_messages` omits earlier turns.
3. No cue + mock `topic_changed=false`: `standalone_query` from the model is used; epoch unchanged.
4. First turn (empty epoch): contextualizer LLM not called; `topic_changed` false.
5. After a switch, six new-topic turns: summarizer invoked; dropped pair is from the **new** epoch only.
6. Cue helper unit tests: `also` matches; `this weekend war games` does not match as a cue; `what about` matches.

Success criterion: prior “party games for 8” then “2-player war games” (no cue) with mocked `topic_changed=true` does not pass “8” / “party” into retrieve/synthesize.

## Success criteria

- New-topic prompts in an existing conversation do not inherit the previous search constraints.
- Anaphoric / comparative follow-ups (`something lighter`, `also …`) still rewrite with history.
- Old messages remain in the transcript; only the rewrite window and summary epoch change.
- No frontend or auth required to verify via API tests / curl.

## Relationship to other docs

- Phase 1: `docs/superpowers/specs/2026-09-04-multi-turn-rag-design.md`
- Guide: `docs/rag-improvement-guide.md` step 4 (topic-switch). Clarification and later steps stay separate specs.
