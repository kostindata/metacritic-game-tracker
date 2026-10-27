from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(1000))
    cover_url: Mapped[str | None] = mapped_column(String(1000))
    # Local copy of cover_url in settings.covers_dir (see app.covers); None = not downloaded.
    cover_file: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    video_url: Mapped[str | None] = mapped_column(String(1000))
    developers: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    publishers: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    genres: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    release_date: Mapped[date | None] = mapped_column(Date)
    # Denormalised "highest across platforms" values used for list sorting.
    best_metascore: Mapped[int | None] = mapped_column(Integer, index=True)
    best_user_score: Mapped[float | None] = mapped_column(Float, index=True)
    # Review counts summed across platforms; drive summary regeneration.
    critic_review_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    user_review_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_crawled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    platforms: Mapped[list["GamePlatform"]] = relationship(
        back_populates="game", cascade="all, delete-orphan", order_by="GamePlatform.platform"
    )
    summaries: Mapped[list["ReviewSummary"]] = relationship(back_populates="game", cascade="all, delete-orphan")


class GamePlatform(Base):
    __tablename__ = "game_platforms"
    __table_args__ = (UniqueConstraint("game_id", "platform", name="uq_game_platform"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(100), index=True)
    platform_slug: Mapped[str | None] = mapped_column(String(100))
    metascore: Mapped[int | None] = mapped_column(Integer)
    user_score: Mapped[float | None] = mapped_column(Float)
    critic_review_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    user_review_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    release_date: Mapped[date | None] = mapped_column(Date)

    game: Mapped[Game] = relationship(back_populates="platforms")


class Review(Base):
    """Raw review snippets fetched for the LLM; replaced on every crawl of the game."""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # critic | user
    platform: Mapped[str | None] = mapped_column(String(100))
    source: Mapped[str | None] = mapped_column(String(255))
    score: Mapped[float | None] = mapped_column(Float)
    quote: Mapped[str] = mapped_column(Text)
    review_date: Mapped[date | None] = mapped_column(Date)


class ReviewSummary(Base):
    __tablename__ = "review_summaries"
    __table_args__ = (UniqueConstraint("game_id", "kind", name="uq_summary_game_kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # critic | user
    # ok | not_enough | pending | error
    status: Mapped[str] = mapped_column(String(20))
    likes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    dislikes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    overview: Mapped[str | None] = mapped_column(Text)
    # Prompt/format version the content was generated with (llm.summarizer.PROMPT_VERSION).
    prompt_version: Mapped[int | None] = mapped_column(Integer)
    # Fingerprint of the written reviews the content was generated from (llm.fingerprint).
    reviews_fingerprint: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(Text)
    # Review count the current content was produced for. Regenerate when it changes.
    review_count: Mapped[int | None] = mapped_column(Integer)
    summary_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    game: Mapped[Game] = relationship(back_populates="summaries")


class CrawlState(Base):
    """Singleton row (id=1) holding the persistent listing cursor."""

    __tablename__ = "crawl_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date)
    phase: Mapped[str] = mapped_column(String(20))  # new_releases | browse
    page: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProcessedToday(Base):
    __tablename__ = "processed_today"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    slug: Mapped[str] = mapped_column(String(255), primary_key=True)
    outcome: Mapped[str] = mapped_column(String(20))  # inserted | updated
    run_id: Mapped[int | None] = mapped_column(Integer)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20))  # scheduled | manual
    # queued | running | success | partial | failed | aborted
    status: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    target: Mapped[int] = mapped_column(Integer, default=20)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    current_game: Mapped[str | None] = mapped_column(String(500))
    note: Mapped[str | None] = mapped_column(Text)


class CrawlError(Base):
    __tablename__ = "crawl_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(Integer, index=True)
    game_slug: Mapped[str | None] = mapped_column(String(255))
    stage: Mapped[str] = mapped_column(String(30))  # listing | game | reviews | llm | run
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int | None] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(10))
    ok: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NewReleaseEntry(Base):
    """The New Releases carousel as first fetched on a crawl day (shown as the UI carousel)."""

    __tablename__ = "new_release_entries"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str | None] = mapped_column(String(500))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GameSimilar(Base):
    """Precomputed top similar games per game (see similarity.refresh_similar)."""

    __tablename__ = "game_similar"

    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, primary_key=True)
    similar_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    score: Mapped[float] = mapped_column(Float)
