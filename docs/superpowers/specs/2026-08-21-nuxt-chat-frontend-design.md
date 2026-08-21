# Nuxt chat frontend (first pass)

Date: 2026-08-21  
Status: approved design (pending implementation plan)

## Goal

Add a Nuxt 4 SSR chat UI for `POST /recommend`. Users type a prompt, see a thread of user bubbles and assistant turns (reasoning + game cards). No saved chat-session list. The FastAPI app stays the recommendation backend.

## Decisions

| Topic | Choice |
|-------|--------|
| App location | `frontend/` (sibling of FastAPI `app/`) |
| Framework | Nuxt 4, SSR enabled (default) |
| UI | Vuetify components + Tailwind v4 layout (`nuxt-tailwind-vuetify`) |
| API access | Nitro proxy `POST /api/recommend` → FastAPI `POST /recommend` |
| CORS | None (browser never calls FastAPI origin) |
| Request body | `{ query }` only; do not send `session_id` |
| Thread | In-page messages; persist in `sessionStorage` (same tab, survives refresh, dies with the tab) |
| Hydration | SSR renders empty thread; load storage in `onMounted` |
| Assistant turn | One message: `reasoning` text, then game cards |
| Filters | Do not show `filters_applied` / `filters_relaxed` |
| State | Page `ref`s + `useChatSession` composable; no Pinia |
| Streaming | No (API is one-shot JSON) |
| History UI | Out of scope |
| Theme | Vuetify default `light` (SSR-safe) |

## Architecture

```
Browser  →  POST /api/recommend { query }
                →  Nitro (runtime config FASTAPI_URL)
                    →  FastAPI POST /recommend { query }
                        →  RecommendResponse
```

Units:

| Unit | Responsibility |
|------|----------------|
| `pages/index.vue` | Layout, composer, send/pending, render thread |
| `useChatSession` | `sessionStorage` load/save/parse; explicit import |
| `server/api/recommend.post.ts` | Forward body to FastAPI; pass through status and JSON error body |
| Hand-written types | Mirror `RecommendRequest` / `RecommendResponse` / `GameRecommendation` |

FastAPI is unchanged.

## UI

Single route `/` in `v-app` / `v-main`. Tailwind: centered column (`max-w-3xl`), scrollable messages, composer at the bottom.

- **Empty state:** one line: “Ask for a board game.”
- **User bubble:** prompt text, aligned end.
- **Assistant turn:** `reasoning`, then a vertical list of `v-card`s: name, player range (`min_players`–`max_players`), play time minutes, per-game `reason`. If `categories` is non-empty, one short line of category names.
- **Composer:** `v-textarea` (single row), Enter sends, Shift+Enter newline, `v-btn` send. Disabled while a request is in flight. Whitespace-only prompt does not send.
- **Loading:** keep input disabled; small pending indicator under the latest user message. Do not insert a fake assistant bubble.
- **Error:** inline on that turn (user message stays). 502 / 503 / 422 / network map to a short human string from the JSON `error`/`detail` when present, else a generic failure line.

No sidebar, theme switcher, or filter chips.

## Session storage

- Key: `boardgame-chat`.
- Value: JSON array of messages (user + assistant + failed-turn error). Do not store the in-flight pending flag.
- Write after every successful append (user send, assistant reply, error on a turn).
- `onMounted`: `JSON.parse`; if missing, not an array, or throw → `[]`.
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
```

`error` on the user message is set when that turn’s request fails; persist it so refresh still shows the failure.

Retry is a new send (same or different text). No auto-retry button in this pass.

## Nitro proxy

- Runtime config `fastapiUrl`, default `http://127.0.0.1:8000`, override with `NUXT_FASTAPI_URL`.
- Handler POSTs `{ query }` to `{fastapiUrl}/recommend`.
- 2xx: return parsed JSON as `RecommendResponse`.
- Non-2xx: return the same status and JSON body when FastAPI sends JSON; otherwise a small `{ error: string }`.
- Network failure to FastAPI: 502 `{ error: "LLM unavailable" }` is wrong here — use `{ error: "Backend unavailable" }` so it is not confused with synthesis 502.

## Tailwind + Vuetify

Follow `nuxt-tailwind-vuetify`: `layers.css` first, then Vuetify styles, then `tailwind.css` (no preflight). Shared breakpoints in `breakpoints.ts`, `settings.scss`, and `@theme`. Disable Vuetify `$utilities` and `$color-pack`. Layout/spacing via Tailwind; components via Vuetify.

## Tests

- Proxy handler: 200 forward; FastAPI 503/502 status and body passed through; fetch throw → 502 `Backend unavailable`.
- `useChatSession`: save/load round-trip; corrupt JSON → `[]`.
- No e2e in this pass.

## Out of scope

Chat session list, `session_id`, localStorage, other-tab sync, streaming, showing applied filters, Docker/Compose for the Nuxt app, auth.
