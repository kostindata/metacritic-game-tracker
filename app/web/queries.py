"""Read-side queries for the UI and the dashboard."""

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.crawler.cursor import crawl_day
from app.crawler.runner import load_cursor
from app.models import (
    CrawlError,
    CrawlRun,
    Game,
    GamePlatform,
    GameSimilar,
    LLMCall,
    NewReleaseEntry,
    ProcessedToday,
    ReviewSummary,
)
from app.similarity import similar_games
from app.monitoring import CrawlLock, list_workers
from app.schedule import next_crawl_time, next_reset_time

PER_PAGE = 24
SORTS = {
    "metascore": Game.best_metascore,
    "userscore": Game.best_user_score,
    "release": Game.release_date,
    "title": func.lower(Game.title),
}


@dataclass
class GamePage:
    games: list[Game]
    total: int
    page: int
    pages: int


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_games(
    session: Session,
    *,
    q: str | None = None,
    platform: str | None = None,
    sort: str = "release",
    order: str = "desc",
    page: int = 1,
) -> GamePage:
    stmt = select(Game)
    if q:
        stmt = stmt.where(Game.title.ilike(f"%{_escape_like(q.strip())}%", escape="\\"))
    if platform:
        stmt = stmt.where(Game.platforms.any(GamePlatform.platform == platform))

    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max(1, math.ceil(total / PER_PAGE))
    page = min(max(1, page), pages)

    column = SORTS.get(sort, Game.release_date)
    ordered = column.asc() if order == "asc" else column.desc()
    stmt = (
        stmt.options(selectinload(Game.platforms))
        .order_by(ordered.nulls_last(), Game.id.desc())
        .offset((page - 1) * PER_PAGE)
        .limit(PER_PAGE)
    )
    return GamePage(list(session.scalars(stmt)), total, page, pages)


def new_release_games(session: Session) -> list[Game]:
    """Games of the most recent New Releases fetch that are in the DB, newest release first."""
    day = session.scalar(select(func.max(NewReleaseEntry.day)))
    if day is None:
        return []
    stmt = (
        select(Game)
        .join(NewReleaseEntry, NewReleaseEntry.slug == Game.slug)
        .where(NewReleaseEntry.day == day)
        .options(selectinload(Game.platforms))
        .order_by(Game.release_date.desc().nulls_last(), NewReleaseEntry.position)
    )
    return list(session.scalars(stmt))


def newest_first(similar: list[tuple[Game, float]]) -> list[tuple[Game, float]]:
    """Order (game, similarity) pairs by release date, newest first; undated games last."""
    return sorted(similar, key=lambda gs: (gs[0].release_date is None, -(gs[0].release_date or date.min).toordinal()))


def similar_for_page(session: Session, game: Game, limit: int = 6) -> list[tuple[Game, float]]:
    """Precomputed similar games; computed live for games crawled since the last refresh."""
    rows = session.execute(
        select(Game, GameSimilar.score)
        .join(GameSimilar, GameSimilar.similar_id == Game.id)
        .where(GameSimilar.game_id == game.id)
        .options(selectinload(Game.platforms))
        .order_by(GameSimilar.rank)
        .limit(limit)
    ).all()
    if rows:
        return [(g, score) for g, score in rows]
    return similar_games(session, game, limit)


def all_platforms(session: Session) -> list[str]:
    return list(session.scalars(select(GamePlatform.platform).distinct().order_by(GamePlatform.platform)))


def get_game(session: Session, slug: str) -> Game | None:
    return session.scalar(
        select(Game)
        .where(Game.slug == slug)
        .options(selectinload(Game.platforms), selectinload(Game.summaries))
    )


# --------------------------------------------------------------------------- dashboard


def cursor_label(cursor) -> str:
    if cursor.phase == "new_releases":
        return f"Новинки, №{cursor.position + 1}"
    return f"Каталог: страница {cursor.page}, №{cursor.position + 1}"


def dashboard_snapshot(session: Session, settings: Settings, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    tz = settings.tz
    today = crawl_day(now, tz, settings.daily_reset_hour)
    day_start = datetime(today.year, today.month, today.day, tzinfo=tz) + timedelta(hours=settings.daily_reset_hour)

    current = session.scalar(
        select(CrawlRun).where(CrawlRun.status.in_(("queued", "running"))).order_by(CrawlRun.id.desc()).limit(1)
    )
    history = list(
        session.scalars(
            select(CrawlRun).where(CrawlRun.status.not_in(("queued", "running"))).order_by(CrawlRun.id.desc()).limit(10)
        )
    )

    outcome_counts = dict(
        session.execute(
            select(ProcessedToday.outcome, func.count()).where(ProcessedToday.day == today).group_by(ProcessedToday.outcome)
        ).all()
    )
    failures_today = session.scalar(
        select(func.coalesce(func.sum(CrawlRun.failed), 0)).where(
            func.coalesce(CrawlRun.started_at, CrawlRun.created_at) >= day_start
        )
    )
    runs_today = session.scalar(
        select(func.count()).select_from(CrawlRun).where(func.coalesce(CrawlRun.started_at, CrawlRun.created_at) >= day_start)
    )

    cursor = load_cursor(session)
    if cursor is None or cursor.day != today:
        cursor_text = "Новинки, №1 (новый день, сбор ещё не начат)"
    else:
        cursor_text = cursor_label(cursor)

    llm_ok = session.scalar(select(func.count()).select_from(LLMCall).where(LLMCall.ok.is_(True))) or 0
    llm_err = session.scalar(select(func.count()).select_from(LLMCall).where(LLMCall.ok.is_(False))) or 0
    pending = session.scalar(
        select(func.count()).select_from(ReviewSummary).where(ReviewSummary.status == "pending")
    ) or 0

    errors = list(session.scalars(select(CrawlError).order_by(CrawlError.id.desc()).limit(20)))

    try:
        workers = list_workers()
        lock_held = CrawlLock().is_locked()
    except Exception:  # noqa: BLE001 - dashboard must render even if Redis is down
        workers, lock_held = [], None

    # Marked active but nobody holds the lock: the worker died mid-run. The row is
    # closed as "aborted" when the next run takes the lock.
    orphaned = current is not None and lock_held is False

    return {
        "now": now,
        "orphaned": orphaned,
        "workers": workers,
        "lock_held": lock_held,
        "current": current,
        "today": {
            "day": today,
            "processed": sum(outcome_counts.values()),
            "inserted": outcome_counts.get("inserted", 0),
            "updated": outcome_counts.get("updated", 0),
            "failures": failures_today or 0,
            "runs": runs_today or 0,
            "cursor": cursor_text,
        },
        "totals": {
            "games": session.scalar(select(func.count()).select_from(Game)) or 0,
            "llm_ok": llm_ok,
            "llm_errors": llm_err,
            "llm_pending": pending,
        },
        "schedule": {
            "next_run": next_crawl_time(now, settings.crawl_schedule_minute, tz),
            "next_reset": next_reset_time(now, settings.daily_reset_hour, tz),
        },
        "errors": errors,
        "history": history,
    }
