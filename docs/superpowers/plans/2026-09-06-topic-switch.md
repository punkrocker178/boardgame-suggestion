# Topic-switch Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop follow-up rewriting from inheriting prior constraints when the user starts a new recommendation topic in the same conversation.

**Architecture:** Hybrid cues skip topic classification and use the phase-1 rewrite. Otherwise one structured LLM call returns `topic_changed`. On switch, discard the model rewrite, persist `topic_started_at` on the switching user timestamp, clear `summary`, and scope recent-load plus rolling summary to that epoch.

**Tech Stack:** FastAPI, SQLAlchemy (Postgres + SQLite tests), LangChain `invoke_structured`, pytest + TestClient

**Spec:** `docs/superpowers/specs/2026-09-05-topic-switch-design.md`

## Global Constraints

- Backend only; no frontend, auth, Alembic, or new dependencies
- `contextualize_query` returns `QueryPlan(standalone_query: str, topic_changed: bool)`, not `str`
- Empty epoch recent messages: no LLM, no cue helper, `QueryPlan(query, False)`
- Cue hit: existing rewrite prompt only; `topic_changed` is always false
- No-cue LLM `topic_changed=true`: discard model `standalone_query`; use raw `query`
- Filters stay in `resolve_filters(standalone_query)`
- Persist user + assistant only after successful synthesis
- On switch, `summary = NULL` and `topic_started_at =` user `created_at` inside `append_turn` (same flush)
- After persist, `count_turns` / `load_turn_pair_at_index` / summarizer use the epoch loaded **before** `append_turn`
- Skip summarizer when `topic_changed` is true
- Rolling summary when not a switch and epoch turns `> 5` (`RECENT_TURN_LIMIT`)
- Failed turns do not persist, move epoch, or clear summary
- Store functions take a `Session`; caller owns the session
- If this tree has no git repo, skip commit steps

---

## File map

| File | Responsibility |
|------|----------------|
| `app/db/models.py` | `Conversation.topic_started_at` |
| `scripts/schema.sql` | Bootstrap column |
| `scripts/migrate_topic_started_at.sql` | One-shot ALTER for existing volumes |
| `docs/database.md` | Operator note |
| `app/services/conversation_store.py` | Epoch-scoped load/count/index; `append_turn(..., topic_changed=)` |
| `tests/test_conversation_store.py` | Epoch + switch persist tests |
| `app/services/contextualizer.py` | `has_followup_cue`, `QueryPlan`, rewrite vs plan prompts |
| `tests/test_contextualizer.py` | Cue helper + plan I/O |
| `app/api/models.py` | `RecommendResponse.topic_changed` |
| `app/api/routes.py` | Wire epoch + `QueryPlan` + payload |
| `tests/test_api.py` | Existing mocks return `QueryPlan`; new switch/cue cases |

---

### Task 1: `topic_started_at` column

**Files:**
- Modify: `app/db/models.py`
- Modify: `scripts/schema.sql`
- Create: `scripts/migrate_topic_started_at.sql`
- Modify: `docs/database.md`
- Test: `tests/test_conversation_store.py`

**Interfaces:**
- Produces: `Conversation.topic_started_at: datetime | None` (nullable, no server default)

- [ ] **Step 1: Write the failing ORM assertion**

In `tests/test_conversation_store.py`, add to `test_create_and_get_conversation`:

```python
        assert loaded.topic_started_at is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_conversation_store.py::test_create_and_get_conversation -v`

Expected: FAIL (`AttributeError: topic_started_at`)

- [ ] **Step 3: Add the ORM column**

In `app/db/models.py`, on `Conversation` immediately after `summary`:

```python
    topic_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

- [ ] **Step 4: Schema + migrate + docs**

In `scripts/schema.sql`, add to `conversations` after `summary TEXT,`:

```sql
    topic_started_at TIMESTAMPTZ,
```

Create `scripts/migrate_topic_started_at.sql`:

```sql
-- One-shot: topic epoch for multi-turn /recommend.
-- Fresh installs: updated scripts/schema.sql already has the column (skip this file).

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS topic_started_at TIMESTAMPTZ;
```

In `docs/database.md`, after the Conversations subsection:

```markdown
### Topic epoch (existing DBs)

Needed for topic-switch on `/recommend`:

1. Apply `scripts/migrate_topic_started_at.sql`.
2. Fresh installs: updated `scripts/schema.sql` already has `conversations.topic_started_at`; skip this migrate script.
```

Do **not** change `scripts/migrate_conversations.sql` (existing volumes already applied it; they need the ALTER).

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_conversation_store.py::test_create_and_get_conversation -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/db/models.py scripts/schema.sql scripts/migrate_topic_started_at.sql docs/database.md tests/test_conversation_store.py
git commit -m "feat: add conversations.topic_started_at for topic epochs"
```

---

### Task 2: Epoch-scoped conversation store

**Files:**
- Modify: `app/services/conversation_store.py`
- Test: `tests/test_conversation_store.py`

**Interfaces:**
- Consumes: `Conversation.topic_started_at`
- Produces:
  - `load_recent_messages(session, conversation_id, max_turns=RECENT_TURN_LIMIT, topic_started_at: datetime | None = None) -> list[Message]`
  - `count_turns(session, conversation_id, topic_started_at: datetime | None = None) -> int`
  - `load_turn_pair_at_index(session, conversation_id, turn_index, topic_started_at: datetime | None = None) -> tuple[Message, Message] | None`
  - `append_turn(..., topic_changed: bool = False) -> None` — if `topic_changed`, set `summary = None` and `topic_started_at =` the user row `created_at` (`now`) in the same flush

- [ ] **Step 1: Write failing store tests**

Append to `tests/test_conversation_store.py`:

```python
def test_epoch_filters_recent_count_and_index() -> None:
    with _session() as session:
        conv = create_conversation(session)
        session.commit()
        for i in range(3):
            append_turn(
                session,
                conv.id,
                user_content=f"old{i}",
                standalone_query=f"old{i}",
                assistant_content=f"a{i}",
                assistant_payload={"reasoning": f"a{i}"},
            )
        session.commit()
        boundary = session.scalars(
            select(Message)
            .where(Message.conversation_id == conv.id, Message.role == "user")
            .order_by(Message.created_at.desc())
        ).first()
        assert boundary is not None
        epoch = boundary.created_at
        assert count_turns(session, conv.id, topic_started_at=epoch) == 1
        recent = load_recent_messages(session, conv.id, topic_started_at=epoch)
        assert [m.content for m in recent if m.role == "user"] == ["old2"]
        pair = load_turn_pair_at_index(session, conv.id, 0, topic_started_at=epoch)
        assert pair is not None
        assert pair[0].content == "old2"
        assert load_turn_pair_at_index(session, conv.id, 1, topic_started_at=epoch) is None


def test_append_turn_topic_changed_sets_epoch_and_clears_summary() -> None:
    with _session() as session:
        conv = create_conversation(session)
        session.commit()
        set_summary(session, conv.id, "Party games for 8.")
        session.commit()
        append_turn(
            session,
            conv.id,
            user_content="party for 8",
            standalone_query="party for 8",
            assistant_content="ok",
            assistant_payload={"reasoning": "ok"},
        )
        append_turn(
            session,
            conv.id,
            user_content="2-player war games",
            standalone_query="2-player war games",
            assistant_content="ok",
            assistant_payload={"reasoning": "ok"},
            topic_changed=True,
        )
        session.commit()
        loaded = get_conversation(session, conv.id)
        assert loaded.summary is None
        switch_user = session.scalars(
            select(Message)
            .where(Message.conversation_id == conv.id, Message.role == "user")
            .order_by(Message.created_at.desc())
        ).first()
        assert loaded.topic_started_at == switch_user.created_at
        recent = load_recent_messages(
            session, conv.id, topic_started_at=loaded.topic_started_at
        )
        assert [m.content for m in recent if m.role == "user"] == ["2-player war games"]
```

Existing tests must keep passing with default `topic_started_at=None` / `topic_changed=False`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_conversation_store.py::test_epoch_filters_recent_count_and_index tests/test_conversation_store.py::test_append_turn_topic_changed_sets_epoch_and_clears_summary -v`

Expected: FAIL (unexpected keyword argument)

- [ ] **Step 3: Implement epoch filtering and switch persist**

Replace `count_turns`, `load_recent_messages`, `append_turn`, and `load_turn_pair_at_index` in `app/services/conversation_store.py` with:

```python
def count_turns(
    session: Session,
    conversation_id: UUID,
    topic_started_at: datetime | None = None,
) -> int:
    where = [
        Message.conversation_id == conversation_id,
        Message.role == "user",
    ]
    if topic_started_at is not None:
        where.append(Message.created_at >= topic_started_at)
    return int(session.scalar(select(func.count()).select_from(Message).where(*where)) or 0)


def load_recent_messages(
    session: Session,
    conversation_id: UUID,
    max_turns: int = RECENT_TURN_LIMIT,
    topic_started_at: datetime | None = None,
) -> list[Message]:
    limit = max_turns * 2
    where = [Message.conversation_id == conversation_id]
    if topic_started_at is not None:
        where.append(Message.created_at >= topic_started_at)
    rows = list(
        session.scalars(
            select(Message).where(*where).order_by(Message.created_at.desc()).limit(limit)
        )
    )
    rows.reverse()
    return rows


def append_turn(
    session: Session,
    conversation_id: UUID,
    *,
    user_content: str,
    standalone_query: str,
    assistant_content: str,
    assistant_payload: dict,
    topic_changed: bool = False,
) -> None:
    now = datetime.now(UTC)
    session.add(
        Message(
            id=uuid4(),
            conversation_id=conversation_id,
            role="user",
            content=user_content,
            standalone_query=standalone_query,
            created_at=now,
        )
    )
    session.add(
        Message(
            id=uuid4(),
            conversation_id=conversation_id,
            role="assistant",
            content=assistant_content,
            payload=assistant_payload,
            created_at=now + timedelta(microseconds=1),
        )
    )
    conv = session.get(Conversation, conversation_id)
    if conv is not None:
        conv.updated_at = now
        conv.version = int(conv.version) + 1
        if topic_changed:
            conv.summary = None
            conv.topic_started_at = now
    session.flush()


def load_turn_pair_at_index(
    session: Session,
    conversation_id: UUID,
    turn_index: int,
    topic_started_at: datetime | None = None,
) -> tuple[Message, Message] | None:
    """0-based turn index among user messages ordered by created_at (epoch-scoped)."""
    if turn_index < 0:
        return None
    where = [Message.conversation_id == conversation_id, Message.role == "user"]
    if topic_started_at is not None:
        where.append(Message.created_at >= topic_started_at)
    user = session.scalars(
        select(Message).where(*where).order_by(Message.created_at.asc()).offset(turn_index).limit(1)
    ).first()
    if user is None:
        return None
    assistant = session.scalars(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.role == "assistant",
            Message.created_at >= user.created_at,
        )
        .order_by(Message.created_at.asc())
        .limit(1)
    ).first()
    if assistant is None:
        return None
    return user, assistant
```

`set_summary` stays `str`-only.

- [ ] **Step 4: Run store tests**

Run: `pytest tests/test_conversation_store.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/conversation_store.py tests/test_conversation_store.py
git commit -m "feat: scope conversation history to topic_started_at"
```

---

### Task 3: Cue helper + `QueryPlan` contextualizer

**Files:**
- Modify: `app/services/contextualizer.py`
- Test: `tests/test_contextualizer.py`

**Interfaces:**
- Consumes: existing `invoke_structured`, `StandaloneQuery` rewrite path
- Produces:
  - `has_followup_cue(query: str) -> bool`
  - `@dataclass(frozen=True) class QueryPlan: standalone_query: str; topic_changed: bool`
  - `contextualize_query(...) -> QueryPlan`

- [ ] **Step 1: Write failing tests**

Replace `tests/test_contextualizer.py` with:

```python
from unittest.mock import MagicMock

from app.services.contextualizer import (
    QueryPlan,
    contextualize_query,
    has_followup_cue,
    summarize_dropped_turn,
)
from app.db.models import Message


def test_has_followup_cue_matches_and_rejects() -> None:
    assert has_followup_cue("also 2-player")
    assert has_followup_cue("something lighter")
    assert has_followup_cue("instead of Catan")
    assert has_followup_cue("more players")
    assert has_followup_cue("what about 2p")
    assert not has_followup_cue("this weekend war games")
    assert not has_followup_cue("war games instead")
    assert not has_followup_cue("2-player war games")


def test_contextualize_skips_llm_without_history() -> None:
    llm = MagicMock()
    result = contextualize_query(llm, query="games for 4", summary=None, recent_messages=[])
    assert result == QueryPlan(standalone_query="games for 4", topic_changed=False)
    llm.assert_not_called()


def test_contextualize_cue_uses_rewrite_only(monkeypatch) -> None:
    from app.services import contextualizer as mod

    calls: list[type] = []

    def fake_invoke(llm, prompt, model, variables):
        calls.append(model)
        return model(standalone_query="light games for 4 players")

    monkeypatch.setattr(mod, "invoke_structured", fake_invoke)
    recent = [
        Message(role="user", content="games for 4 players"),
        Message(role="assistant", content="Here are some options."),
    ]
    result = contextualize_query(
        MagicMock(),
        query="something lighter",
        summary=None,
        recent_messages=recent,
    )
    assert result == QueryPlan(
        standalone_query="light games for 4 players", topic_changed=False
    )
    assert calls == [mod.StandaloneQuery]


def test_contextualize_no_cue_switch_discards_rewrite(monkeypatch) -> None:
    from app.services import contextualizer as mod

    def fake_invoke(llm, prompt, model, variables):
        return model(topic_changed=True, standalone_query="smuggle party games for 8")

    monkeypatch.setattr(mod, "invoke_structured", fake_invoke)
    recent = [
        Message(role="user", content="party games for 8"),
        Message(role="assistant", content="Here are some options."),
    ]
    result = contextualize_query(
        MagicMock(),
        query="2-player war games",
        summary=None,
        recent_messages=recent,
    )
    assert result == QueryPlan(standalone_query="2-player war games", topic_changed=True)


def test_contextualize_no_cue_follow_up_uses_model_rewrite(monkeypatch) -> None:
    from app.services import contextualizer as mod

    def fake_invoke(llm, prompt, model, variables):
        return model(topic_changed=False, standalone_query="cooperative games for 4")

    monkeypatch.setattr(mod, "invoke_structured", fake_invoke)
    recent = [
        Message(role="user", content="games for 4 players"),
        Message(role="assistant", content="Here are some options."),
    ]
    result = contextualize_query(
        MagicMock(),
        query="cooperative games",
        summary=None,
        recent_messages=recent,
    )
    assert result == QueryPlan(
        standalone_query="cooperative games for 4", topic_changed=False
    )


def test_summarize_dropped_turn(monkeypatch) -> None:
    from app.services import contextualizer as mod

    def fake_invoke(llm, prompt, model, variables):
        return model(summary="User wants 4-player games.")

    monkeypatch.setattr(mod, "invoke_structured", fake_invoke)
    out = summarize_dropped_turn(
        MagicMock(),
        prior_summary=None,
        user_content="games for 4",
        assistant_content="Suggested Catan.",
    )
    assert out == "User wants 4-player games."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_contextualizer.py -v`

Expected: FAIL (`ImportError` / `has_followup_cue` missing / `QueryPlan` missing)

- [ ] **Step 3: Implement contextualizer**

Replace `app/services/contextualizer.py` with:

```python
from __future__ import annotations

import re
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.db.models import Message
from app.helpers.llm_parsing import invoke_structured

_CUE_PHRASES = (
    "what about",
    "how about",
    "same but",
    "same as",
    "but with",
    "but for",
    "except",
    "without the",
    "instead of",
    "lighter",
    "heavier",
    "shorter",
    "longer",
    "simpler",
    "cheaper",
    "more",
    "less",
    "another",
    "other",
    "similar",
    "quicker",
    "easier",
    "harder",
    "faster",
    "slower",
    "bigger",
    "smaller",
)
_CUE_WORDS = re.compile(
    r"\b(?:those ones|it|that|those|them|also|they)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QueryPlan:
    standalone_query: str
    topic_changed: bool


class StandaloneQuery(BaseModel):
    standalone_query: str = Field(min_length=1)


class TopicSwitchOutput(BaseModel):
    topic_changed: bool
    standalone_query: str = Field(min_length=1)


class ConversationSummary(BaseModel):
    summary: str = Field(min_length=1)


CONTEXTUALIZE_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You rewrite board-game recommendation follow-ups into one standalone search prompt.\n"
            "Rules:\n"
            "1. Resolve pronouns and short follow-ups using the conversation summary and recent messages.\n"
            "2. Preserve earlier constraints (players, time, complexity, similar-to, categories) unless the "
            "follow-up clearly replaces them.\n"
            "3. Do not invent game names or constraints absent from the conversation.\n"
            "4. Return only the standalone_query field.",
        ),
        (
            "human",
            "Summary:\n{summary}\n\nRecent messages:\n{recent_messages}\n\nCurrent user message:\n{query}",
        ),
    ]
)

PLAN_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You plan a board-game recommendation follow-up.\n"
            "Rules:\n"
            "1. Set topic_changed true when the current message is a new recommendation request that must "
            "not keep prior players, time, complexity/weight, similar-to, or categories.\n"
            "2. Otherwise set topic_changed false and rewrite into one standalone search prompt. Preserve "
            "earlier constraints unless the follow-up clearly replaces them.\n"
            "3. Do not invent game names or filters absent from the conversation.\n"
            "4. Return JSON only with topic_changed and standalone_query.",
        ),
        (
            "human",
            "Summary:\n{summary}\n\nRecent messages:\n{recent_messages}\n\nCurrent user message:\n{query}",
        ),
    ]
)

SUMMARY_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "Update the running summary of a board-game recommendation chat. "
            "Keep stable preferences and constraints. Be concise.",
        ),
        (
            "human",
            "Prior summary:\n{prior_summary}\n\nDropped turn:\nUser: {user_content}\nAssistant: {assistant_content}",
        ),
    ]
)


def has_followup_cue(query: str) -> bool:
    lowered = query.lower()
    if any(phrase in lowered for phrase in _CUE_PHRASES):
        return True
    return _CUE_WORDS.search(query) is not None


def _format_recent(messages: list[Message]) -> str:
    lines: list[str] = []
    for m in messages:
        lines.append(f"{m.role}: {m.content}")
    return "\n".join(lines) if lines else "(none)"


def contextualize_query(
    llm: BaseChatModel,
    *,
    query: str,
    summary: str | None,
    recent_messages: list[Message],
) -> QueryPlan:
    if not recent_messages:
        return QueryPlan(standalone_query=query, topic_changed=False)
    variables = {
        "summary": summary or "(none)",
        "recent_messages": _format_recent(recent_messages),
        "query": query,
    }
    if has_followup_cue(query):
        result = invoke_structured(llm, CONTEXTUALIZE_PROMPT, StandaloneQuery, variables)
        return QueryPlan(standalone_query=result.standalone_query.strip(), topic_changed=False)
    result = invoke_structured(llm, PLAN_PROMPT, TopicSwitchOutput, variables)
    if result.topic_changed:
        return QueryPlan(standalone_query=query, topic_changed=True)
    return QueryPlan(standalone_query=result.standalone_query.strip(), topic_changed=False)


def summarize_dropped_turn(
    llm: BaseChatModel,
    *,
    prior_summary: str | None,
    user_content: str,
    assistant_content: str,
) -> str:
    result = invoke_structured(
        llm,
        SUMMARY_PROMPT,
        ConversationSummary,
        {
            "prior_summary": prior_summary or "(none)",
            "user_content": user_content,
            "assistant_content": assistant_content,
        },
    )
    return result.summary.strip()
```

- [ ] **Step 4: Run contextualizer tests**

Run: `pytest tests/test_contextualizer.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/contextualizer.py tests/test_contextualizer.py
git commit -m "feat: detect topic switch in query contextualizer"
```

---

### Task 4: Wire `/recommend` + update existing API mocks

**Files:**
- Modify: `app/api/models.py`
- Modify: `app/api/routes.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: `QueryPlan`, `load_recent_messages(..., topic_started_at=)`, `append_turn(..., topic_changed=)`, `count_turns` / `load_turn_pair_at_index` with pre-append epoch
- Produces: `RecommendResponse.topic_changed: bool`; assistant payload includes `topic_changed`

- [ ] **Step 1: Write failing response-field assertion**

In `tests/test_api.py` `test_recommend_first_turn_skips_contextualizer_llm`, after `assert data["standalone_query"] == "games for 4 players"`:

```python
    assert data["topic_changed"] is False
```

In `test_recommend_follow_up_uses_standalone_query`, change the fake to return `QueryPlan` and assert `topic_changed` is false:

```python
    from app.services.contextualizer import QueryPlan

    def fake_contextualize(llm, *, query, summary, recent_messages):
        assert recent_messages
        return QueryPlan("light complexity games for 4 players", False)
```

```python
    assert response.json()["topic_changed"] is False
```

In `test_recommend_refreshes_summary_when_window_exceeded`:

```python
    from app.services.contextualizer import QueryPlan

    monkeypatch.setattr(
        "app.api.routes.contextualize_query",
        lambda *a, **k: QueryPlan(k["query"], False),
    )
```

In `test_recommend` (the live-ish 200 test around line 150), after the `standalone_query` key check:

```python
    assert "topic_changed" in data
```

- [ ] **Step 2: Run those tests to verify they fail**

Run: `pytest tests/test_api.py::test_recommend_first_turn_skips_contextualizer_llm tests/test_api.py::test_recommend_follow_up_uses_standalone_query tests/test_api.py::test_recommend_refreshes_summary_when_window_exceeded -v`

Expected: FAIL (`topic_changed` missing and/or `QueryPlan` used as standalone string)

- [ ] **Step 3: Add response field and wire the route**

In `app/api/models.py`, on `RecommendResponse`:

```python
    topic_changed: bool = False
```

Replace the conversation load + contextualize + persist/summary block in `app/api/routes.py` `recommend` so it matches this (retrieve/synthesize unchanged except they use `plan.standalone_query`):

```python
    with session_factory() as session:
        conv = get_conversation(session, request.conversation_id)
        if conv is None:
            raise HTTPException(status_code=404, detail={"error": "Conversation not found"})
        epoch = conv.topic_started_at
        recent = load_recent_messages(
            session, request.conversation_id, topic_started_at=epoch
        )
        summary = conv.summary

    if recent and llm is None:
        raise HTTPException(status_code=502, detail={"error": "LLM unavailable"})

    try:
        plan = contextualize_query(
            llm,
            query=request.query,
            summary=summary,
            recent_messages=recent,
        )
    except (APIConnectionError, APIStatusError, ValidationError, json.JSONDecodeError) as exc:
        logger.exception("Contextualizer failed")
        raise HTTPException(status_code=502, detail={"error": "LLM unavailable"}) from exc

    standalone_query = plan.standalone_query
```

Keep `resolve_filters` / `retrieve_games` / `synthesize_recommendations` on `standalone_query`.

Build the response with `topic_changed=plan.topic_changed`.

Payload:

```python
    payload = {
        "reasoning": response.reasoning,
        "recommendations": [r.model_dump() for r in response.recommendations],
        "filters_applied": applied.model_dump(),
        "filters_relaxed": filters_relaxed,
        "standalone_query": standalone_query,
        "topic_changed": plan.topic_changed,
    }
```

Persist + rolling summary:

```python
    with session_factory() as session:
        append_turn(
            session,
            request.conversation_id,
            user_content=request.query,
            standalone_query=standalone_query,
            assistant_content=response.reasoning,
            assistant_payload=payload,
            topic_changed=plan.topic_changed,
        )
        session.commit()

        if not plan.topic_changed:
            turns = count_turns(
                session, request.conversation_id, topic_started_at=epoch
            )
            if turns > RECENT_TURN_LIMIT:
                dropped_index = turns - RECENT_TURN_LIMIT - 1
                pair = load_turn_pair_at_index(
                    session,
                    request.conversation_id,
                    dropped_index,
                    topic_started_at=epoch,
                )
                if pair is not None:
                    user_msg, assistant_msg = pair
                    try:
                        conv_after = get_conversation(session, request.conversation_id)
                        new_summary = summarize_dropped_turn(
                            llm,
                            prior_summary=conv_after.summary if conv_after else None,
                            user_content=user_msg.content,
                            assistant_content=assistant_msg.content,
                        )
                        set_summary(session, request.conversation_id, new_summary)
                        session.commit()
                    except Exception:
                        logger.exception("Summary refresh failed; keeping prior summary")
```

`epoch` must be the value loaded **before** `append_turn` (closure from the first session block).

- [ ] **Step 4: Run existing API tests**

Run: `pytest tests/test_api.py tests/test_api_models.py tests/test_contextualizer.py tests/test_conversation_store.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/api/models.py app/api/routes.py tests/test_api.py
git commit -m "feat: expose topic_changed on recommend and persist epoch"
```

---

### Task 5: Spec API cases (cue, switch, epoch summary)

**Files:**
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: Task 4 route behavior

- [ ] **Step 1: Write failing API tests**

Append to `tests/test_api.py` (reuse the same `SynthesisOutput` / `GameRecommendation` stub pattern as existing recommend tests):

```python
def _ok_synthesis() -> SynthesisOutput:
    return SynthesisOutput(
        recommendations=[
            GameRecommendation(
                name="Catan",
                reason="ok",
                min_players=3,
                max_players=4,
                play_time_minutes=90,
                categories=["strategy"],
            )
        ],
        reasoning="ok",
    )


@patch("app.api.routes.synthesize_recommendations")
@patch("app.api.routes.resolve_filters")
def test_recommend_cue_keeps_epoch(
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
    monkeypatch,
) -> None:
    from uuid import UUID

    from sqlalchemy import select

    from app.api import routes
    from app.db.models import Conversation, Message
    from app.services.contextualizer import QueryPlan

    mock_resolve.return_value = ExtractedFilters(player_count=4)
    mock_synthesize.return_value = _ok_synthesis()
    cid = _conversation_id(client)
    client.post("/recommend", json={"conversation_id": cid, "query": "party games for 8"})

    def fake_contextualize(llm, *, query, summary, recent_messages):
        assert recent_messages
        return QueryPlan("lighter party games for 8", False)

    monkeypatch.setattr("app.api.routes.contextualize_query", fake_contextualize)
    response = client.post(
        "/recommend",
        json={"conversation_id": cid, "query": "something lighter"},
    )
    assert response.status_code == 200
    assert response.json()["topic_changed"] is False
    factory = routes.get_session_factory()
    with factory() as session:
        conv = session.get(Conversation, UUID(cid))
        assert conv.topic_started_at is None
        users = list(
            session.scalars(
                select(Message).where(
                    Message.conversation_id == UUID(cid), Message.role == "user"
                )
            )
        )
        assert len(users) == 2


@patch("app.api.routes.synthesize_recommendations")
@patch("app.api.routes.resolve_filters")
def test_recommend_topic_switch_uses_raw_query_and_moves_epoch(
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
    monkeypatch,
) -> None:
    from uuid import UUID

    from sqlalchemy import select

    from app.api import routes
    from app.db.models import Conversation, Message
    from app.services.contextualizer import QueryPlan

    mock_resolve.return_value = ExtractedFilters()
    mock_synthesize.return_value = _ok_synthesis()
    cid = _conversation_id(client)
    client.post(
        "/recommend", json={"conversation_id": cid, "query": "party games for 8"}
    )

    def fake_contextualize(llm, *, query, summary, recent_messages):
        return QueryPlan(query, True)

    monkeypatch.setattr("app.api.routes.contextualize_query", fake_contextualize)
    response = client.post(
        "/recommend",
        json={"conversation_id": cid, "query": "2-player war games"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["topic_changed"] is True
    assert data["standalone_query"] == "2-player war games"
    assert mock_resolve.call_args.args[1] == "2-player war games"
    assert mock_synthesize.call_args.args[1] == "2-player war games"
    assert "8" not in mock_resolve.call_args.args[1]
    assert "party" not in mock_resolve.call_args.args[1].lower()

    factory = routes.get_session_factory()
    with factory() as session:
        conv = session.get(Conversation, UUID(cid))
        assert conv.summary is None
        switch_user = session.scalars(
            select(Message)
            .where(Message.conversation_id == UUID(cid), Message.role == "user")
            .order_by(Message.created_at.desc())
        ).first()
        assert conv.topic_started_at == switch_user.created_at

    seen: list[list[str]] = []

    def record_recent(llm, *, query, summary, recent_messages):
        seen.append([m.content for m in recent_messages])
        return QueryPlan(query, False)

    monkeypatch.setattr("app.api.routes.contextualize_query", record_recent)
    client.post(
        "/recommend",
        json={"conversation_id": cid, "query": "something lighter"},
    )
    assert seen
    assert "party games for 8" not in seen[0]


@patch("app.api.routes.synthesize_recommendations")
@patch("app.api.routes.resolve_filters")
def test_recommend_no_cue_without_switch_keeps_epoch(
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
    monkeypatch,
) -> None:
    from uuid import UUID

    from app.api import routes
    from app.db.models import Conversation
    from app.services.contextualizer import QueryPlan

    mock_resolve.return_value = ExtractedFilters()
    mock_synthesize.return_value = _ok_synthesis()
    cid = _conversation_id(client)
    client.post("/recommend", json={"conversation_id": cid, "query": "games for 4"})

    monkeypatch.setattr(
        "app.api.routes.contextualize_query",
        lambda *a, **k: QueryPlan("cooperative games for 4", False),
    )
    response = client.post(
        "/recommend",
        json={"conversation_id": cid, "query": "cooperative games"},
    )
    assert response.json()["standalone_query"] == "cooperative games for 4"
    assert response.json()["topic_changed"] is False
    factory = routes.get_session_factory()
    with factory() as session:
        conv = session.get(Conversation, UUID(cid))
        assert conv.topic_started_at is None


@patch("app.api.routes.synthesize_recommendations")
@patch("app.api.routes.resolve_filters")
@patch("app.api.routes.summarize_dropped_turn", return_value="New topic summary.")
def test_recommend_summary_after_switch_drops_new_epoch_only(
    mock_summarize: MagicMock,
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
    monkeypatch,
) -> None:
    from app.services.contextualizer import QueryPlan

    mock_resolve.return_value = ExtractedFilters()
    mock_synthesize.return_value = _ok_synthesis()
    cid = _conversation_id(client)
    client.post("/recommend", json={"conversation_id": cid, "query": "party games for 8"})

    plans = iter(
        [QueryPlan("2-player war games", True)]
        + [QueryPlan(f"new{i}", False) for i in range(6)]
    )

    def fake_contextualize(llm, *, query, summary, recent_messages):
        return next(plans)

    monkeypatch.setattr("app.api.routes.contextualize_query", fake_contextualize)
    client.post(
        "/recommend", json={"conversation_id": cid, "query": "2-player war games"}
    )
    mock_summarize.reset_mock()
    for i in range(6):
        client.post("/recommend", json={"conversation_id": cid, "query": f"new{i}"})
    assert mock_summarize.called
    dropped_users = [c.kwargs["user_content"] for c in mock_summarize.call_args_list]
    assert "party games for 8" not in dropped_users
    assert "2-player war games" in dropped_users
```

- [ ] **Step 2: Run the new tests**

Run: `pytest tests/test_api.py::test_recommend_cue_keeps_epoch tests/test_api.py::test_recommend_topic_switch_uses_raw_query_and_moves_epoch tests/test_api.py::test_recommend_no_cue_without_switch_keeps_epoch tests/test_api.py::test_recommend_summary_after_switch_drops_new_epoch_only -v`

Expected: PASS (Task 4 already wired the route). If a dropped-user assertion fails, fix the route index to match the spec (epoch-scoped `turns - 5 - 1`), not the test.

After the switch turn plus six `new{i}` posts, epoch length is 7. Summarizer runs on epoch count 6 and 7. Dropped users are the switch query then `new0` — never `party games for 8`.

- [ ] **Step 3: Run the full related suite**

Run: `pytest tests/test_api.py tests/test_api_models.py tests/test_contextualizer.py tests/test_conversation_store.py -v`

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_api.py
git commit -m "test: cover topic-switch recommend paths"
```

---

## Self-review

1. **Spec coverage:** column/migrate/docs ✓; store epoch + append switch ✓; cues + QueryPlan + discard rewrite ✓; route epoch-before-append + skip summary on switch + rolling refresh ✓; API `topic_changed` ✓; tests 1–6 + success criterion ✓; out-of-scope left out ✓
2. **Placeholders:** none
3. **Types:** `QueryPlan`, `topic_started_at: datetime | None`, `append_turn(..., topic_changed: bool = False)` consistent across tasks; existing API mocks updated in Task 4 before new cases in Task 5
