from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


def set_summary(session: Session, conversation_id: UUID, summary: str) -> None:
    conv = session.get(Conversation, conversation_id)
    if conv is None:
        return
    conv.summary = summary
    conv.updated_at = datetime.now(UTC)
    conv.version = int(conv.version) + 1
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
