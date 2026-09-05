import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    TypeDecorator,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class IntArray(TypeDecorator):
    """Postgres INTEGER[]; JSON list on SQLite for unit tests."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(ARRAY(Integer))
        return dialect.type_descriptor(JSON())


class CrawlStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    year_published: Mapped[int | None] = mapped_column(Integer)
    rank: Mapped[int | None] = mapped_column(Integer)
    bayes_average: Mapped[float | None] = mapped_column(Numeric(10, 5))
    average: Mapped[float | None] = mapped_column(Numeric(10, 5))
    users_rated: Mapped[int | None] = mapped_column(Integer)
    is_expansion: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    abstracts_rank: Mapped[int | None] = mapped_column(Integer)
    cgs_rank: Mapped[int | None] = mapped_column(Integer)
    childrensgames_rank: Mapped[int | None] = mapped_column(Integer)
    familygames_rank: Mapped[int | None] = mapped_column(Integer)
    partygames_rank: Mapped[int | None] = mapped_column(Integer)
    strategygames_rank: Mapped[int | None] = mapped_column(Integer)
    thematic_rank: Mapped[int | None] = mapped_column(Integer)
    wargames_rank: Mapped[int | None] = mapped_column(Integer)

    description: Mapped[str | None] = mapped_column(Text)
    min_players: Mapped[int | None] = mapped_column(Integer)
    max_players: Mapped[int | None] = mapped_column(Integer)
    playing_time: Mapped[int | None] = mapped_column(Integer)
    min_play_time: Mapped[int | None] = mapped_column(Integer)
    max_play_time: Mapped[int | None] = mapped_column(Integer)
    min_age: Mapped[int | None] = mapped_column(Integer)
    weight: Mapped[float | None] = mapped_column(Numeric(4, 2))
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    best_with_players: Mapped[list[int] | None] = mapped_column(IntArray)
    recommended_with_players: Mapped[list[int] | None] = mapped_column(IntArray)

    crawl_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CrawlStatus.PENDING
    )
    crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    crawl_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_crawl_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    categories: Mapped[list["GameCategory"]] = relationship(
        back_populates="game", cascade="all, delete-orphan"
    )
    mechanics: Mapped[list["GameMechanic"]] = relationship(
        back_populates="game", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_games_crawl_status", "crawl_status"),
        Index("idx_games_rank", "rank"),
    )


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class Mechanic(Base):
    __tablename__ = "mechanics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class GameCategory(Base):
    __tablename__ = "game_categories"

    game_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("categories.id"), primary_key=True
    )

    game: Mapped[Game] = relationship(back_populates="categories")
    category: Mapped[Category] = relationship()


class GameMechanic(Base):
    __tablename__ = "game_mechanics"

    game_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    mechanic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("mechanics.id"), primary_key=True
    )

    game: Mapped[Game] = relationship(back_populates="mechanics")
    mechanic: Mapped[Mechanic] = relationship()


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
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

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
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
