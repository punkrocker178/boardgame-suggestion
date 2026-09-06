# Nuxt chat frontend (first pass)

Date: 2026-08-21 (updated 2026-09-05 for multi-turn)  
Status: approved design (pending implementation plan)

## Goal

Add a Nuxt 4 SSR chat UI for multi-turn `POST /recommend`. Users type prompts and follow-ups in one thread, see user bubbles and assistant turns (reasoning + game cards). No saved chat-session list. The FastAPI app owns conversation memory and contextualizes follow-ups server-side.

Depends on: `docs/superpowers/specs/2026-09-04-multi-turn-rag-design.md`.

## Decisions

| Topic | Choice |
|-------|--------|
| App location | `frontend/` (sibling of FastAPI `app/`) |
| Framework | Nuxt 4, SSR enabled (default) |
| UI | Vuetify components + Tailwind v4 layout (`nuxt-tailwind-vuetify`) |
| API access | Nitro proxies: `POST /api/conversations`, `POST /api/recommend` |
| CORS | None (browser never calls FastAPI origin) |
| Conversation | `POST /conversations` before the first `/recommend` in a tab; reuse `conversation_id` for follow-ups |
| Request body | `{ query, conversation_id }` — send only the latest user text, not prior turns |
| Thread display | In-page messages; persist in `sessionStorage` (same tab, survives refresh, dies with the tab) |
| Server memory | FastAPI loads recent turns + summary from DB; client does not send message history |
| Hydration | SSR renders empty thread; load storage in `onMounted` |
| Assistant turn | One message: `reasoning` text, then game cards |
| Hidden response fields | Do not show `filters_applied`, `filters_relaxed`, `standalone_query`, or `topic_changed` (when present) |
| State | Page `ref`s + `useChatSession` composable; no Pinia |
| Streaming | No (API is one-shot JSON) |
| History UI | Out of scope (no conversation list, no server transcript reload) |
| Theme | Vuetify default `light` (SSR-safe) |

## Architecture

```
Browser
  →  POST /api/conversations {}
       →  FastAPI POST /conversations → { id }
  →  POST /api/recommend { query, conversation_id }
       →  Nitro (runtime config FASTAPI_URL)
           →  FastAPI POST /recommend { query, conversation_id }
               →  load recent turns + summary; contextualize; pipeline
               →  RecommendResponse
```

Units:

| Unit | Responsibility |
|------|----------------|
| `pages/index.vue` | Layout, composer, send/pending, render thread |
| `useChatSession` | `sessionStorage` load/save/parse; hold `conversationId`; explicit import |
| `useRecommend` (or inline in page) | Ensure conversation exists; call recommend; handle 404 recovery |
| `server/api/conversations.post.ts` | Forward to FastAPI `POST /conversations`; pass through status/body |
| `server/api/recommend.post.ts` | Forward body to FastAPI; pass through status and JSON error body |
| Hand-written types | Mirror `ConversationCreate*` / `RecommendRequest` / `RecommendResponse` / `GameRecommendation` |

## UI

Single route `/` in `v-app` / `v-main`. Tailwind: centered column (`max-w-3xl`), scrollable messages, composer at the bottom.

- **Empty state:** one line: “Ask for a board game.”
- **User bubble:** prompt text, aligned end.
- **Assistant turn:** `reasoning`, then a vertical list of `v-card`s: name, player range (`min_players`–`max_players`), play time minutes, per-game `reason`. If `categories` is non-empty, one short line of category names.
- **Composer:** `v-textarea` (single row), Enter sends, Shift+Enter newline, `v-btn` send. Disabled while a request is in flight. Whitespace-only prompt does not send.
- **Loading:** keep input disabled; small pending indicator under the latest user message. Do not insert a fake assistant bubble.
- **Error:** inline on that turn (user message stays). 404 / 502 / 503 / 422 / network map to a short human string from the JSON `error`/`detail` when present, else a generic failure line.

No sidebar, theme switcher, or filter chips.

## Conversation lifecycle

1. **First send in a tab:** if `conversationId` is missing, `POST /api/conversations` with `{}`, store returned `id`, then `POST /api/recommend`.
2. **Follow-up send:** reuse stored `conversationId`; send only the new `query`.
3. **Refresh:** restore `conversationId` and messages from `sessionStorage`; follow-ups continue using server-side history for contextualization even though the UI rehydrates from local storage.
4. **404 on recommend** (`Conversation not found`, e.g. DB reset): clear stored `conversationId`, create a new conversation, retry the same recommend **once**. If the retry fails, show the error on the user message (do not loop).
5. **Failed turns:** server does not persist failed LLM turns; the client still appends the user bubble and sets `error` locally so the thread reads correctly.

Do not create a conversation on bare page load (avoids orphan rows when the user never sends).

## Session storage

- Key: `boardgame-chat`.
- Value: JSON object `{ conversationId: string | null, messages: ChatMessage[] }`. Do not store the in-flight pending flag.
- Write after every state change (`conversationId` set, user send, assistant reply, error on a turn).
- `onMounted`: `JSON.parse`; on missing, invalid shape, or throw → `{ conversationId: null, messages: [] }`.
- **Legacy shape:** if the stored value is a bare array (pre–multi-turn), treat it as `{ conversationId: null, messages: array }`.
- SSR HTML is always the empty thread so hydration matches. Restored messages appear after mount.

Message shapes:

```ts
type UserMessage = { role: 'user'; content: string; error?: string }
type AssistantMessage = {
  role: 'assistant'
  reasoning: string
  recommendations: GameRecommendation[]
}
type ChatMessage = UserMessage | AssistantMessage

type StoredChat = {
  conversationId: string | null
  messages: ChatMessage[]
}
```

`error` on the user message is set when that turn’s request fails; persist it so refresh still shows the failure.

Retry is a new send (same or different text). No auto-retry button in this pass.

## Nitro proxy

- Runtime config `fastapiUrl`, default `http://127.0.0.1:8000`, override with `NUXT_FASTAPI_URL`.

### `POST /api/conversations`

- Forward optional body to `{fastapiUrl}/conversations`.
- 201: return `{ id }` as `ConversationCreateResponse`.
- Non-2xx: pass through status and JSON body when present; else `{ error: string }`.
- Network failure: 502 `{ error: "Backend unavailable" }`.

### `POST /api/recommend`

- Forward `{ query, conversation_id }` to `{fastapiUrl}/recommend` (map `conversationId` → `conversation_id` if the composable uses camelCase internally).
- 2xx: return parsed JSON as `RecommendResponse` (includes `conversation_id`, `standalone_query`; UI ignores the extras).
- Non-2xx: return the same status and JSON body when FastAPI sends JSON; otherwise a small `{ error: string }`.
- Network failure to FastAPI: 502 `{ error: "Backend unavailable" }` (not `"LLM unavailable"` — that status is for synthesis failures from FastAPI).

## Tailwind + Vuetify

Follow `nuxt-tailwind-vuetify`: `layers.css` first, then Vuetify styles, then `tailwind.css` (no preflight). Shared breakpoints in `breakpoints.ts`, `settings.scss`, and `@theme`. Disable Vuetify `$utilities` and `$color-pack`. Layout/spacing via Tailwind; components via Vuetify.

## Tests

- `conversations` proxy: 201 forward; non-2xx pass-through; fetch throw → 502 `Backend unavailable`.
- `recommend` proxy: 200 forward with `conversation_id`; FastAPI 404/503/502 status and body passed through; fetch throw → 502 `Backend unavailable`.
- `useChatSession`: save/load round-trip for `StoredChat`; legacy array migration; corrupt JSON → empty state.
- Recommend flow (unit or composable test): first send creates conversation then recommends; follow-up reuses id; 404 triggers one recreate+retry.
- No e2e in this pass.

## Out of scope

Chat session list, reloading transcript from the server, `localStorage`, other-tab sync, streaming, showing applied filters or `standalone_query`, Docker/Compose for the Nuxt app, auth, “new chat” control (new tab = new conversation).
