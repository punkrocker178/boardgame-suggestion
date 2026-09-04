# Multi-turn Conversational RAG (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist anonymous conversations and rewrite follow-up prompts into standalone queries before the existing filter → retrieve → synthesize path.

**Architecture:** `POST /conversations` creates a row. `POST /recommend` requires `conversation_id`, loads summary + last 5 turns, contextualizes (skip LLM on first turn), runs today’s pipeline on `standalone_query`, appends messages, best-effort summary refresh.

**Tech Stack:** FastAPI, SQLAlchemy (Postgres + SQLite tests), LangChain `invoke_structured`, pytest + TestClient

**Spec:** `docs/superpowers/specs/2026-09-04-multi-turn-rag-design.md`

## Global Constraints

- `conversation_id` required on `/recommend`; remove `session_id`
- First turn (no prior messages): skip contextualizer LLM; `standalone_query = query`
- Filters stay in `resolve_filters(standalone_query)` — contextualizer returns only `{ standalone_query }`
- Persist user + assistant only after successful synthesis
- Summary refresh is best-effort (log + keep old summary on failure)
- Recent window: **5** turns (user+assistant pair); `RECENT_TURN_LIMIT = 5`
- No Alembic — `scripts/schema.sql` + `scripts/migrate_conversations.sql` + `docs/database.md`
- No new dependencies
- Store functions take a `Session` argument (caller owns the session / test factory)
- Pass `standalone_query` into both `resolve_filters` and `retrieve_games` / `synthesize_recommendations`
- If this tree has no git repo, skip commit steps

---

## File map

| File | Responsibility |
|------|----------------|
| `app/db/models.py` | `Conversation`, `Message` ORM |
| `app/db/__init__.py` | Export new models |
| `scripts/schema.sql` | Bootstrap DDL for conversations/messages |
| `scripts/migrate_conversations.sql` | One-shot for existing Postgres volumes |
| `docs/database.md` | Operator note for migrate script |
| `app/conversation_store.py` | create / get / load recent / append / update summary |
| `app/contextualizer.py` | `contextualize_query`, `summarize_dropped_turn` |
| `app/models.py` | Request/response API models |
| `app/main.py` | `POST /conversations`; wire multi-turn into `recommend` |
| `tests/test_conversation_store.py` | Store unit tests |
| `tests/test_contextualizer.py` | First-turn skip + follow-up prompt/IO |
| `tests/test_api.py` | Require `conversation_id`; multi-turn API cases |

---

### Task 1: Conversation ORM + schema + migrate

**Files:**
- Modify: `app/db/models.py`
- Modify: `app/db/__init__.py`
- Modify: `scripts/schema.sql`
- Create: `scripts/migrate_conversations.sql`
- Modify: `docs/database.md`
- Test: `tests/test_conversation_store.py` (create via ORM smoke — written fully in Task 2; this task only needs models importable)

**Interfaces:**
- Produces: `Conversation`, `Message` mapped classes; UUID PKs; `Message.payload` as JSON

- [ ] **Step 1: Write a failing import/ORM smoke test**

Create `tests/test_conversation_store.py`:

```python
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Conversation, Message


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_conversation_message_round_trip() -> None:
    with _session() as session:
        conv = Conversation(id=uuid4(), created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        session.add(conv)
        session.flush()
        session.add(
            Message(
                id=uuid4(),
                conversation_id=conv.id,
                role="user",
                content="games for 4",
                standalone_query="games for 4",
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
        loaded = session.scalars(select(Message)).one()
        assert loaded.role == "user"
        assert loaded.conversation_id == conv.id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_conversation_store.py::test_conversation_message_round_trip -v`

Expected: FAIL (`Conversation` / `Message` not defined)

- [ ] **Step 3: Add ORM models**

Append to `app/db/models.py` (add imports: `Uuid` from sqlalchemy, `uuid4` from uuid, `Any` from typing if needed):

```python
class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    title: Mapped[str | None] = mapped_column(String(200))
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    standalone_query: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
```

Use `import uuid` and `from uuid import uuid4`. Match existing `DateTime(timezone=True)` / `func.now()` style used on `Game` if present; otherwise mirror the snippet above.

Export from `app/db/__init__.py`:

```python
from app.db.models import (
    Base,
    Category,
    Conversation,
    Game,
    GameCategory,
    GameMechanic,
    Mechanic,
    Message,
)

__all__ = [
    "Base",
    "Category",
    "Conversation",
    "Game",
    "GameCategory",
    "GameMechanic",
    "Mechanic",
    "Message",
    "get_engine",
    "get_session_factory",
    "init_db",
]
```

- [ ] **Step 4: Update `scripts/schema.sql`**

Append:

```sql
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY,
    title VARCHAR(200),
    summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version INT NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    standalone_query TEXT,
    payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
```

- [ ] **Step 5: Add migrate script + docs**

Create `scripts/migrate_conversations.sql`:

```sql
-- One-shot: add conversation tables for multi-turn RAG.
-- Fresh installs: use updated scripts/schema.sql (skip this file).

CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY,
    title VARCHAR(200),
    summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version INT NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    standalone_query TEXT,
    payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
```

In `docs/database.md`, after the pg_trgm subsection, add:

```markdown
### Conversations (existing DBs)

Needed for multi-turn `/recommend`:

1. Apply `scripts/migrate_conversations.sql`.
2. Fresh installs: updated `scripts/schema.sql` already creates the tables; skip this migrate script.
```

- [ ] **Step 6: Run ORM smoke test**

Run: `pytest tests/test_conversation_store.py::test_conversation_message_round_trip -v`

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/db/models.py app/db/__init__.py scripts/schema.sql scripts/migrate_conversations.sql docs/database.md tests/test_conversation_store.py
git commit -m "feat: add conversation and message tables"
```

---

### Task 2: `conversation_store`

**Files:**
- Create: `app/conversation_store.py`
- Modify: `tests/test_conversation_store.py`

**Interfaces:**
- Consumes: `Conversation`, `Message`, SQLAlchemy `Session`
- Produces:
  - `RECENT_TURN_LIMIT = 5`
  - `create_conversation(session, title: str | None = None) -> Conversation`
  - `get_conversation(session, conversation_id: UUID) -> Conversation | None`
  - `load_recent_messages(session, conversation_id: UUID, max_turns: int = RECENT_TURN_LIMIT) -> list[Message]` (oldest→newest, at most `max_turns * 2` messages)
  - `count_turns(session, conversation_id: UUID) -> int` (user message count)
  - `append_turn(session, conversation_id, *, user_content, standalone_query, assistant_content, assistant_payload: dict) -> None`
  - `set_summary(session, conversation_id, summary: str) -> None` (bumps `updated_at` + `version`)

- [ ] **Step 1: Write failing store tests**

Add to `tests/test_conversation_store.py`:

```python
from uuid import UUID

from app.conversation_store import (
    RECENT_TURN_LIMIT,
    append_turn,
    count_turns,
    create_conversation,
    get_conversation,
    load_recent_messages,
    set_summary,
)


def test_create_and_get_conversation() -> None:
    with _session() as session:
        conv = create_conversation(session, title="night")
        session.commit()
        loaded = get_conversation(session, conv.id)
        assert loaded is not None
        assert loaded.title == "night"
        assert get_conversation(session, UUID("00000000-0000-0000-0000-000000000001")) is None


def test_append_and_load_recent_window() -> None:
    with _session() as session:
        conv = create_conversation(session)
        session.commit()
        for i in range(RECENT_TURN_LIMIT + 1):
            append_turn(
                session,
                conv.id,
                user_content=f"q{i}",
                standalone_query=f"sq{i}",
                assistant_content=f"a{i}",
                assistant_payload={"reasoning": f"a{i}"},
            )
        session.commit()
        assert count_turns(session, conv.id) == RECENT_TURN_LIMIT + 1
        recent = load_recent_messages(session, conv.id)
        assert len(recent) == RECENT_TURN_LIMIT * 2
        assert recent[0].content == "q1"
        assert recent[-1].content == f"a{RECENT_TURN_LIMIT}"


def test_set_summary() -> None:
    with _session() as session:
        conv = create_conversation(session)
        session.commit()
        set_summary(session, conv.id, "User wants 4-player games.")
        session.commit()
        assert get_conversation(session, conv.id).summary == "User wants 4-player games."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_conversation_store.py -v`

Expected: FAIL (import `conversation_store`)

- [ ] **Step 3: Implement `app/conversation_store.py`**

```python
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Conversation, Message

RECENT_TURN_LIMIT = 5


def create_conversation(session: Session, title: str | None = None) -> Conversation:
    now = datetime.now(UTC)
    conv = Conversation(id=uuid4(), title=title, created_at=now, updated_at=now, version=1)
    session.add(conv)
    session.flush()
    return conv


def get_conversation(session: Session, conversation_id: UUID) -> Conversation | None:
    return session.get(Conversation, conversation_id)


def count_turns(session: Session, conversation_id: UUID) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(Message).where(
                Message.conversation_id == conversation_id,
                Message.role == "user",
            )
        )
        or 0
    )


def load_recent_messages(
    session: Session,
    conversation_id: UUID,
    max_turns: int = RECENT_TURN_LIMIT,
) -> list[Message]:
    limit = max_turns * 2
    rows = list(
        session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.role.desc())
            .limit(limit)
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
            created_at=now,
        )
    )
    conv = session.get(Conversation, conversation_id)
    if conv is not None:
        conv.updated_at = now
        conv.version = int(conv.version) + 1
    session.flush()


def set_summary(session: Session, conversation_id: UUID, summary: str) -> None:
    conv = session.get(Conversation, conversation_id)
    if conv is None:
        return
    conv.summary = summary
    conv.updated_at = datetime.now(UTC)
    conv.version = int(conv.version) + 1
    session.flush()


def load_turn_pair_at_index(
    session: Session, conversation_id: UUID, turn_index: int
) -> tuple[Message, Message] | None:
    """0-based turn index among user messages ordered by created_at."""
    users = list(
        session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.role == "user")
            .order_by(Message.created_at.asc())
        )
    )
    if turn_index < 0 or turn_index >= len(users):
        return None
    user = users[turn_index]
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

Note on `load_recent_messages` ordering: with identical `created_at` for a pair, order user before assistant when reversing. Prefer inserting user then assistant (as above) and ordering by `created_at.asc(), role.asc()` with `user` < `assistant` alphabetically — **fix the store** to order ascending with a stable tie-break:

```python
.order_by(Message.created_at.asc(), Message.role.desc())  # user after assistant alphabetically — bad
```

Use explicit role order via CASE, or bump assistant `created_at` by 1µs. Simplest fix in `append_turn`: set assistant `created_at = now + timedelta(microseconds=1)`. Update Step 3 implementation accordingly so `load_recent_messages` can use:

```python
select(Message)
.where(Message.conversation_id == conversation_id)
.order_by(Message.created_at.desc())
.limit(limit)
```

then reverse.

- [ ] **Step 4: Run store tests**

Run: `pytest tests/test_conversation_store.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/conversation_store.py tests/test_conversation_store.py
git commit -m "feat: add conversation_store helpers"
```

---

### Task 3: API models

**Files:**
- Modify: `app/models.py`
- Test: covered by API tests in Task 5; add a tiny unit check here if desired

**Interfaces:**
- Produces:
  - `ConversationCreateRequest(title: str | None = None)`
  - `ConversationCreateResponse(id: UUID)`
  - `RecommendRequest(query, conversation_id: UUID)` — no `session_id`
  - `RecommendResponse` gains `conversation_id: UUID`, `standalone_query: str`

- [ ] **Step 1: Write failing model validation test**

Add to `tests/test_llm_parsing.py` or create `tests/test_api_models.py`:

```python
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models import RecommendRequest, RecommendResponse


def test_recommend_request_requires_conversation_id() -> None:
    with pytest.raises(ValidationError):
        RecommendRequest(query="hello")


def test_recommend_request_accepts_conversation_id() -> None:
    cid = uuid4()
    req = RecommendRequest(query="hello", conversation_id=cid)
    assert req.conversation_id == cid
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_api_models.py -v`

Expected: FAIL (unexpected kwargs / missing field behavior)

- [ ] **Step 3: Update `app/models.py`**

```python
from uuid import UUID

class ConversationCreateRequest(BaseModel):
    title: str | None = None


class ConversationCreateResponse(BaseModel):
    id: UUID


class RecommendRequest(BaseModel):
    query: str = Field(min_length=1)
    conversation_id: UUID


class RecommendResponse(BaseModel):
    recommendations: list[GameRecommendation]
    reasoning: str
    filters_applied: FiltersApplied
    filters_relaxed: bool = False
    conversation_id: UUID
    standalone_query: str
```

Remove `session_id`.

- [ ] **Step 4: Run model tests**

Run: `pytest tests/test_api_models.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/models.py tests/test_api_models.py
git commit -m "feat: require conversation_id on recommend API models"
```

---

### Task 4: Contextualizer + summarizer

**Files:**
- Create: `app/contextualizer.py`
- Create: `tests/test_contextualizer.py`

**Interfaces:**
- Consumes: `BaseChatModel`, `invoke_structured`, recent `Message` list, optional summary
- Produces:
  - `class StandaloneQuery(BaseModel): standalone_query: str`
  - `class ConversationSummary(BaseModel): summary: str`
  - `contextualize_query(llm, *, query, summary, recent_messages) -> str`
  - `summarize_dropped_turn(llm, *, prior_summary, user_content, assistant_content) -> str`

- [ ] **Step 1: Write failing tests**

```python
from unittest.mock import MagicMock

from app.contextualizer import contextualize_query, summarize_dropped_turn
from app.db.models import Message


def test_contextualize_skips_llm_without_history() -> None:
    llm = MagicMock()
    result = contextualize_query(llm, query="games for 4", summary=None, recent_messages=[])
    assert result == "games for 4"
    llm.assert_not_called()
    # invoke_structured must not be used — patch it if contextualize imports it


def test_contextualize_calls_llm_with_history(monkeypatch) -> None:
    from app import contextualizer as mod

    calls: list[dict] = []

    def fake_invoke(llm, prompt, model, variables):
        calls.append(variables)
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
    assert result == "light games for 4 players"
    assert "something lighter" in calls[0]["query"]
    assert "games for 4 players" in calls[0]["recent_messages"]


def test_summarize_dropped_turn(monkeypatch) -> None:
    from app import contextualizer as mod

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

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_contextualizer.py -v`

Expected: FAIL (module missing)

- [ ] **Step 3: Implement `app/contextualizer.py`**

```python
from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.db.models import Message
from app.llm_parsing import invoke_structured


class StandaloneQuery(BaseModel):
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
) -> str:
    if not recent_messages:
        return query
    result = invoke_structured(
        llm,
        CONTEXTUALIZE_PROMPT,
        StandaloneQuery,
        {
            "summary": summary or "(none)",
            "recent_messages": _format_recent(recent_messages),
            "query": query,
        },
    )
    return result.standalone_query.strip()


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
git add app/contextualizer.py tests/test_contextualizer.py
git commit -m "feat: add query contextualizer and turn summarizer"
```

---

### Task 5: Wire `POST /conversations` + multi-turn `/recommend`

**Files:**
- Modify: `app/main.py`
- Modify: `tests/test_api.py`
- Create: helper usage inside tests

**Interfaces:**
- Consumes: store + contextualizer + existing pipeline
- Produces: `201` create; recommend flow per spec

- [ ] **Step 1: Update existing API tests to create a conversation first**

Add helper at top of `tests/test_api.py`:

```python
def _conversation_id(client: TestClient) -> str:
    response = client.post("/conversations", json={})
    assert response.status_code == 201
    return response.json()["id"]
```

Change every `client.post("/recommend", json={...})` to include `"conversation_id": _conversation_id(client)` except the empty-query 422 case (can omit id or include both — empty query still 422).

For `test_recommend_empty_query_returns_422`, keep `{"query": ""}` (missing/blank still 422).

Add new tests:

```python
def test_create_conversation_returns_id(client: TestClient) -> None:
    response = client.post("/conversations", json={"title": "x"})
    assert response.status_code == 201
    assert "id" in response.json()


def test_recommend_unknown_conversation_returns_404(client: TestClient) -> None:
    response = client.post(
        "/recommend",
        json={
            "conversation_id": "00000000-0000-0000-0000-000000000099",
            "query": "any",
        },
    )
    assert response.status_code == 404


@patch("app.main.synthesize_recommendations")
@patch("app.main.resolve_filters")
@patch("app.contextualizer.invoke_structured")
def test_recommend_first_turn_skips_contextualizer_llm(
    mock_invoke: MagicMock,
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
) -> None:
    mock_resolve.return_value = ExtractedFilters(player_count=4)
    mock_synthesize.return_value = SynthesisOutput(
        recommendations=[
            GameRecommendation(
                name="Catan",
                reason="Fits.",
                min_players=3,
                max_players=4,
                play_time_minutes=90,
                categories=["strategy"],
            )
        ],
        reasoning="ok",
    )
    cid = _conversation_id(client)
    response = client.post(
        "/recommend",
        json={"conversation_id": cid, "query": "games for 4 players"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["conversation_id"] == cid
    assert data["standalone_query"] == "games for 4 players"
    mock_invoke.assert_not_called()
    mock_resolve.assert_called_once()
    assert mock_resolve.call_args.args[1] == "games for 4 players"


@patch("app.main.synthesize_recommendations")
@patch("app.main.resolve_filters")
def test_recommend_follow_up_uses_standalone_query(
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
    monkeypatch,
) -> None:
    mock_resolve.return_value = ExtractedFilters(player_count=4)
    mock_synthesize.return_value = SynthesisOutput(
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
    cid = _conversation_id(client)
    client.post("/recommend", json={"conversation_id": cid, "query": "games for 4 players"})

    def fake_contextualize(llm, *, query, summary, recent_messages):
        assert recent_messages
        return "light complexity games for 4 players"

    monkeypatch.setattr("app.main.contextualize_query", fake_contextualize)
    response = client.post(
        "/recommend",
        json={"conversation_id": cid, "query": "something lighter"},
    )
    assert response.status_code == 200
    assert response.json()["standalone_query"] == "light complexity games for 4 players"
    assert mock_resolve.call_args.args[1] == "light complexity games for 4 players"
```

Add summary test:

```python
@patch("app.main.synthesize_recommendations")
@patch("app.main.resolve_filters")
@patch("app.main.summarize_dropped_turn", return_value="User likes 4-player games.")
def test_recommend_refreshes_summary_when_window_exceeded(
    mock_summarize: MagicMock,
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
    monkeypatch,
) -> None:
    mock_resolve.return_value = ExtractedFilters()
    mock_synthesize.return_value = SynthesisOutput(
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
    monkeypatch.setattr("app.main.contextualize_query", lambda *a, **k: k["query"])
    cid = _conversation_id(client)
    for i in range(6):
        client.post("/recommend", json={"conversation_id": cid, "query": f"q{i}"})
    assert mock_summarize.called
```

- [ ] **Step 2: Run API tests — expect failures**

Run: `pytest tests/test_api.py -v`

Expected: FAIL (missing endpoints / fields)

- [ ] **Step 3: Implement `app/main.py` wiring**

Imports to add:

```python
from uuid import UUID

from app.contextualizer import contextualize_query, summarize_dropped_turn
from app.conversation_store import (
    RECENT_TURN_LIMIT,
    append_turn,
    create_conversation,
    get_conversation,
    load_recent_messages,
    load_turn_pair_at_index,
    count_turns,
    set_summary,
)
from app.models import (
    ConversationCreateRequest,
    ConversationCreateResponse,
    HealthResponse,
    RecommendRequest,
    RecommendResponse,
)
```

Add endpoint:

```python
@app.post("/conversations", response_model=ConversationCreateResponse, status_code=201)
def create_conversation_endpoint(
    request: ConversationCreateRequest | None = None,
) -> ConversationCreateResponse:
    body = request or ConversationCreateRequest()
    session_factory = get_session_factory()
    with session_factory() as session:
        conv = create_conversation(session, title=body.title)
        session.commit()
        return ConversationCreateResponse(id=conv.id)
```

Replace `recommend` body with:

```python
@app.post("/recommend", response_model=RecommendResponse)
def recommend(request: RecommendRequest) -> RecommendResponse:
    logger.info(
        "POST /recommend query=%r conversation_id=%s",
        request.query,
        request.conversation_id,
    )
    if not app_state.indexing_ok or app_state.indexed_games == 0:
        raise HTTPException(status_code=503, detail={"error": "No games indexed"})

    settings = app_state.settings
    llm = app_state.llm
    session_factory = get_session_factory()

    with session_factory() as session:
        conv = get_conversation(session, request.conversation_id)
        if conv is None:
            raise HTTPException(status_code=404, detail={"error": "Conversation not found"})
        recent = load_recent_messages(session, request.conversation_id)
        summary = conv.summary

    try:
        standalone_query = contextualize_query(
            llm,
            query=request.query,
            summary=summary,
            recent_messages=recent,
        )
    except (APIConnectionError, APIStatusError, Exception) as exc:
        # Only treat provider/parse failures as 502; re-raise HTTPException unchanged if any
        if isinstance(exc, HTTPException):
            raise
        logger.exception("Contextualizer failed")
        raise HTTPException(status_code=502, detail={"error": "LLM unavailable"}) from exc

    filters = resolve_filters(llm, standalone_query)

    chroma_dir = Path(settings.chroma_persist_dir)
    vector_store = get_vector_store(chroma_dir, get_embeddings(settings))
    with session_factory() as session:
        candidates, filters_relaxed = retrieve_games(
            session, vector_store, filters, standalone_query, top_k=5
        )

    if llm is None:
        raise HTTPException(status_code=502, detail={"error": "LLM unavailable"})

    try:
        synthesis = synthesize_recommendations(
            llm, standalone_query, filters, candidates
        )
    except (APIConnectionError, APIStatusError) as exc:
        logger.exception("LLM provider unreachable during synthesis")
        raise HTTPException(status_code=502, detail={"error": "LLM unavailable"}) from exc

    applied = filters_to_applied(filters)
    response = RecommendResponse(
        recommendations=synthesis.recommendations,
        reasoning=synthesis.reasoning,
        filters_applied=applied,
        filters_relaxed=filters_relaxed,
        conversation_id=request.conversation_id,
        standalone_query=standalone_query,
    )

    payload = {
        "reasoning": response.reasoning,
        "recommendations": [r.model_dump() for r in response.recommendations],
        "filters_applied": applied.model_dump(),
        "filters_relaxed": filters_relaxed,
        "standalone_query": standalone_query,
    }

    with session_factory() as session:
        append_turn(
            session,
            request.conversation_id,
            user_content=request.query,
            standalone_query=standalone_query,
            assistant_content=response.reasoning,
            assistant_payload=payload,
        )
        session.commit()

        turns = count_turns(session, request.conversation_id)
        if turns > RECENT_TURN_LIMIT:
            dropped_index = turns - RECENT_TURN_LIMIT - 1
            pair = load_turn_pair_at_index(session, request.conversation_id, dropped_index)
            if pair is not None:
                user_msg, assistant_msg = pair
                try:
                    new_summary = summarize_dropped_turn(
                        llm,
                        prior_summary=get_conversation(session, request.conversation_id).summary,
                        user_content=user_msg.content,
                        assistant_content=assistant_msg.content,
                    )
                    set_summary(session, request.conversation_id, new_summary)
                    session.commit()
                except Exception:
                    logger.exception("Summary refresh failed; keeping prior summary")

    logger.info(
        "POST /recommend complete recommendations=%d filters_relaxed=%s",
        len(response.recommendations),
        filters_relaxed,
    )
    return response
```

Narrow the contextualizer `except`: catch `(APIConnectionError, APIStatusError, ValueError, ValidationError)` rather than bare `Exception` if that matches project style — do **not** swallow programming bugs. Prefer:

```python
from pydantic import ValidationError
...
except (APIConnectionError, APIStatusError, ValidationError, json.JSONDecodeError) as exc:
```

`contextualize_query` with `llm is None` on follow-up: if `recent_messages` non-empty and `llm is None`, return 502 before calling. First turn does not need llm for contextualize.

```python
if recent and llm is None:
    raise HTTPException(status_code=502, detail={"error": "LLM unavailable"})
```

- [ ] **Step 4: Run all related tests**

Run: `pytest tests/test_api.py tests/test_conversation_store.py tests/test_contextualizer.py tests/test_api_models.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_api.py
git commit -m "feat: multi-turn recommend with conversation contextualization"
```

---

### Task 6: Final verification

**Files:** none new

- [ ] **Step 1: Run full unit suite**

Run: `pytest -q`

Expected: PASS (update any remaining `/recommend` callers in tests if grep finds them)

- [ ] **Step 2: Grep for stale `session_id`**

Run: `rg "session_id" -g "*.py" -g "*.md"`

Expected: only historical docs / this plan / guide; no live API field

- [ ] **Step 3: Commit any leftover test fixes**

```bash
git add -u
git commit -m "test: finish multi-turn RAG phase 1 coverage"
```

Skip empty commit if clean.

---

## Self-review checklist (author)

1. **Spec coverage:** persist ✓, load context ✓, contextualizer ✓, create-then-require id ✓, summary ✓, errors ✓, out-of-scope left out ✓  
2. **No placeholders** in task steps  
3. **Types:** `conversation_id: UUID`, `standalone_query: str`, store signatures consistent across tasks
