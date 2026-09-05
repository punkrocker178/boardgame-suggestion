from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.conversation_store import (
    RECENT_TURN_LIMIT,
    append_turn,
    count_turns,
    create_conversation,
    get_conversation,
    load_recent_messages,
    load_turn_pair_at_index,
    set_summary,
)
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


def test_load_turn_pair_at_index() -> None:
    with _session() as session:
        conv = create_conversation(session)
        session.commit()
        for i in range(3):
            append_turn(
                session,
                conv.id,
                user_content=f"q{i}",
                standalone_query=f"sq{i}",
                assistant_content=f"a{i}",
                assistant_payload={"reasoning": f"a{i}"},
            )
        session.commit()
        pair = load_turn_pair_at_index(session, conv.id, 1)
        assert pair is not None
        user, assistant = pair
        assert user.content == "q1"
        assert assistant.content == "a1"
        assert load_turn_pair_at_index(session, conv.id, -1) is None
        assert load_turn_pair_at_index(session, conv.id, 3) is None


def test_set_summary() -> None:
    with _session() as session:
        conv = create_conversation(session)
        session.commit()
        set_summary(session, conv.id, "User wants 4-player games.")
        session.commit()
        assert get_conversation(session, conv.id).summary == "User wants 4-player games."
