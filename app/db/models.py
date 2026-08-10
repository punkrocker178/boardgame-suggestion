from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


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


class GameCategory(Base):
    __tablename__ = "game_categories"

    game_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    category: Mapped[str] = mapped_column(String(255), primary_key=True)

    game: Mapped[Game] = relationship(back_populates="categories")


class GameMechanic(Base):
    __tablename__ = "game_mechanics"

    game_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    mechanic: Mapped[str] = mapped_column(String(255), primary_key=True)

    game: Mapped[Game] = relationship(back_populates="mechanics")
