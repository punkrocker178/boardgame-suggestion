# Multi-turn conversational RAG (phase 1)

Date: 2026-09-04  
Status: approved design (pending implementation plan)

## Goal

Make `POST /recommend` multi-turn: clients create a conversation, send follow-ups in that conversation, and the server rewrites ambiguous prompts into standalone queries before the existing filter → retrieve → synthesize pipeline. Backend only.

## Decisions

| Topic | Choice |
|-------|--------|
| Scope | Multi-turn follow-ups only (not multi-intent in one message) |
| Surface | Backend API only; Nuxt UI out of scope |
| Ownership | Anonymous conversations (no user/auth) |
| Create flow | `POST /conversations` first; every `/recommend` requires an existing id |
| Chat entry | Extend `POST /recommend` (no separate `/chat`) |
| Memory | Last 5 turns + rolling LLM summary of older content |
| Contextualizer output | `standalone_query` string only |
| Filters | Still owned by existing `resolve_filters` on the standalone query |
| Pipeline reuse | Unchanged: `resolve_filters` → `retrieve_games` → `synthesize_recommendations` |
| Request field | Replace unused `session_id` with required `conversation_id` |
| Schema tooling | Update `scripts/schema.sql` + ORM + one-shot migrate SQL; no Alembic |
| Failed turns | Do not persist (append only after successful synthesis) |

## Out of scope (later phases)

Topic-switch detection, clarification path, multi-intent / query decomposition, hybrid lexical retrieval, reranking, grounded citations / `MessageSources`, semantic long-term memory, frontend, real auth, evaluation harness beyond unit tests.

## Architecture

```
POST /conversations
  → insert Conversation → { id }

POST /recommend { conversation_id, query }
  → load Conversation (404 if missing)
  → load summary + last 5 turns
  → contextualize → standalone_query
       (skip LLM on first turn: standalone_query = query)
  → resolve_filters(standalone_query)
  → retrieve_games(...)
  → synthesize_recommendations(...)
  → append user + assistant Messages
  → refresh summary if older turns exist (best-effort)
  → RecommendResponse + conversation_id + standalone_query
```

Units:

| Unit | Responsibility |
|------|----------------|
| `Conversation` / `Message` ORM | Persist sessions and turns |
| `app/conversation_store.py` | create; get; load recent; append turn; update summary |
| `app/contextualizer.py` | History + summary + current → standalone query |
| `app/main.py` | `POST /conversations`; wire multi-turn into `recommend` |
| Existing extract / retrieve / synthesize | Unchanged consumers of the standalone string |

## Data model

### `conversations`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | Client-facing conversation id |
| `title` | VARCHAR(200) NULL | Optional; unused by phase 1 logic |
| `summary` | TEXT NULL | Rolling summary of turns outside the recent window |
| `created_at` | TIMESTAMPTZ | |
| `updated_at` | TIMESTAMPTZ | Bumped on each successful turn |
| `version` | INT NOT NULL DEFAULT 1 | Optimistic bump on update; no conflict API yet |

No `user_id` in phase 1.

### `messages`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `conversation_id` | UUID FK → conversations | Cascade delete |
| `role` | VARCHAR(20) | `user` or `assistant` |
| `content` | TEXT NOT NULL | User: raw query. Assistant: `reasoning` text for summarizer/context |
| `standalone_query` | TEXT NULL | Set on user rows after contextualization |
| `payload` | JSON/JSONB NULL | Assistant only: full response fields (see below) |
| `created_at` | TIMESTAMPTZ | |

Assistant `payload`:

```json
{
  "reasoning": "...",
  "recommendations": [ /* GameRecommendation objects */ ],
  "filters_applied": { /* FiltersApplied */ },
  "filters_relaxed": false,
  "standalone_query": "..."
}
```

SQLite tests: use SQLAlchemy `JSON` (same dialect pattern as existing array helpers). Postgres: JSONB preferred in `schema.sql`.

Ship `scripts/migrate_conversations.sql` for existing volumes; fresh installs get tables from updated `scripts/schema.sql`. Document operator steps in `docs/database.md`.

## API

### `POST /conversations`

Request (optional body):

```json
{ "title": "Friday night" }
```

Response `201`:

```json
{ "id": "<uuid>" }
```

### `POST /recommend`

Request:

```json
{
  "conversation_id": "<uuid>",
  "query": "something lighter"
}
```

`conversation_id` is required. Remove `session_id`.

Response: existing `RecommendResponse` fields plus:

```json
{
  "conversation_id": "<uuid>",
  "standalone_query": "light complexity board games for 4 players",
  "recommendations": [],
  "reasoning": "...",
  "filters_applied": {},
  "filters_relaxed": false
}
```

### Errors

| Case | Status |
|------|--------|
| Missing/invalid body fields | 422 |
| Unknown `conversation_id` | 404 |
| Contextualizer / summarizer / synthesis LLM failure | 502 (same family as today’s synthesis failures) |

## Contextualizer

Input: current `query`, conversation `summary` (may be null/empty), recent messages (up to 5 user/assistant pairs, oldest→newest).

Rules:

1. Resolve pronouns and short follow-ups into one standalone recommendation prompt.
2. Preserve hard constraints implied by earlier turns (player count, time, complexity, similar-to, etc.) when the follow-up does not contradict them.
3. If the follow-up clearly replaces a constraint, use the new one.
4. Do not invent game names or filters absent from the conversation.
5. Return JSON only: `{ "standalone_query": "..." }`.

First turn (no prior messages): do not call the LLM; `standalone_query = query`.

Filter extraction stays in `resolve_filters(standalone_query)` — the contextualizer does not emit `ExtractedFilters`.

## Summary refresh

- Recent window for contextualizer: last **5** turns (a turn = one user message + its assistant reply).
- After a successful append, if messages exist outside that window, call a small summarizer LLM with prior summary + the messages dropping out of the window; write result to `conversations.summary`.
- If summarizer fails: log, keep previous summary, still return the recommend response (turn already persisted).

## Error handling & persistence

- Validate conversation exists before contextualization.
- Persist user + assistant messages only after successful synthesis.
- Do not store failed LLM turns.
- Summary update is best-effort after persist.

## Testing

Pytest, existing FastAPI test style (SQLite + mocked LLM):

1. Create conversation → `/recommend` with unknown id → 404.
2. First turn: contextualizer not called; two message rows; response includes `conversation_id` and `standalone_query == query`.
3. Follow-up: mocked contextualizer; assert retrieve/synthesize path receives standalone query; four message rows.
4. Summary: after enough turns, summarizer invoked (mock); `conversations.summary` updated.
5. Existing filter/retrieve/API tests updated for required `conversation_id` (tests create a conversation first).

Success criterion for rewrite quality: unit-test contextualizer I/O with a fixed history — e.g. prior “games for 4 players” + “something lighter” → standalone mentions light/low complexity and 4 players (assert on mocked LLM prompt contents and/or parsed structured output in a pure function test).

## Success criteria

- Follow-ups work without the client rewriting the full prompt.
- Empty-history path behavior matches today’s single-turn quality (same extract → retrieve → synthesize).
- Clients cannot use `/recommend` without creating a conversation first.
- No frontend or auth required to verify via API tests / curl.

## Relationship to other docs

- Guide: `docs/rag-improvement-guide.md` steps 1–3 (persist, load context, contextualizer). Step 4: `docs/superpowers/specs/2026-09-05-topic-switch-design.md`. Later guide steps are separate specs.
- Nuxt chat design (`2026-08-21-nuxt-chat-frontend-design.md`) updated 2026-09-05 to adopt `conversation_id` and server-side turn memory.
