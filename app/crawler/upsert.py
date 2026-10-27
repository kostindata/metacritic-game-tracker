"""Database writes for a crawled game: upsert, platforms, reviews and summary planning."""

from datetime import datetime

from sqlalchemy import delete, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.llm.fingerprint import reviews_fingerprint
from app.models import Game, GamePlatform, Review, ReviewSummary
from app.scraper.parsers import GameDetail, ReviewData

NOT_ENOUGH_MESSAGE = "Not enough reviews yet"
KINDS = ("critic", "user")


def _max(values):
    present = [v for v in values if v is not None]
    return max(present) if present else None


def upsert_game(
    session: Session, detail: GameDetail, now: datetime, cover_file: str | None = None
) -> tuple[Game, bool]:
    """Insert or update a game by slug. Returns (game, inserted).

    A platform whose user-score request failed this time keeps its stored user score and count,
    so a transient error never wipes data; the game's best score and totals use the merged values.
    """
    values = dict(
        slug=detail.slug,
        title=detail.title,
        url=detail.url,
        cover_url=detail.cover_url,
        cover_file=cover_file,
        description=detail.description,
        video_url=detail.video_url,
        developers=detail.developers,
        publishers=detail.publishers,
        genres=detail.genres,
        release_date=detail.release_date,
        best_metascore=_max(p.metascore for p in detail.platforms),
        critic_review_total=sum(p.critic_review_count for p in detail.platforms),
        last_crawled_at=now,
    )
    stmt = (
        insert(Game)
        .values(**values, first_seen_at=now)
        .on_conflict_do_update(index_elements=[Game.slug], set_=values)
        # xmax = 0 only for freshly inserted rows
        .returning(Game.id, literal_column("(xmax = 0)").label("inserted"))
    )
    row = session.execute(stmt).one()
    game = session.get(Game, row.id, populate_existing=True)
    assert game is not None

    existing = {p.platform: p for p in game.platforms}
    seen = set()
    for pd in detail.platforms:
        seen.add(pd.name)
        gp = existing.get(pd.name)
        if gp is None:
            gp = GamePlatform(platform=pd.name, user_review_count=0)
            game.platforms.append(gp)
        gp.platform_slug = pd.slug
        gp.metascore = pd.metascore
        if not pd.user_stats_failed:
            gp.user_score = pd.user_score
            gp.user_review_count = pd.user_review_count
        gp.critic_review_count = pd.critic_review_count
        gp.release_date = pd.release_date
    for name, gp in existing.items():
        if name not in seen:
            game.platforms.remove(gp)
    game.best_user_score = _max(gp.user_score for gp in game.platforms)
    game.user_review_total = sum(gp.user_review_count or 0 for gp in game.platforms)
    session.flush()
    return game, bool(row.inserted)


def replace_reviews(session: Session, game_id: int, kind: str, reviews: list[ReviewData]) -> None:
    session.execute(delete(Review).where(Review.game_id == game_id, Review.kind == kind))
    session.add_all(
        Review(
            game_id=game_id,
            kind=kind,
            platform=r.platform,
            source=r.source,
            score=r.score,
            quote=r.quote,
            review_date=r.review_date,
        )
        for r in reviews
    )


def summary_action(
    *,
    existing_status: str | None,
    existing_fingerprint: str | None,
    new_fingerprint: str,
    fetched_reviews: int,
    min_reviews: int,
) -> str:
    """Decide what a crawl does with a summary: 'not_enough', 'regenerate' or 'keep'.

    A ready summary is kept, and reused on every page view, until the set of written reviews
    changes (different fingerprint). Anything that is not a ready summary for the current
    reviews is marked for generation, which happens when someone opens the game.
    """
    if fetched_reviews < min_reviews:
        return "keep" if existing_status == "not_enough" else "not_enough"
    if existing_status == "ok" and existing_fingerprint == new_fingerprint:
        return "keep"
    return "regenerate"


def plan_summary(
    session: Session, game: Game, kind: str, reviews: list[ReviewData], min_reviews: int
) -> str:
    """Update the summary row for (game, kind) after a crawl; never calls the LLM.

    A summary that needs (re)generation is marked "outdated" and keeps its old content; it is
    generated on demand when someone opens the game (see llm.on_demand). Returns the action.
    """
    summary = session.scalar(
        select(ReviewSummary).where(ReviewSummary.game_id == game.id, ReviewSummary.kind == kind)
    )
    fingerprint = reviews_fingerprint(reviews)
    action = summary_action(
        existing_status=summary.status if summary else None,
        existing_fingerprint=summary.reviews_fingerprint if summary else None,
        new_fingerprint=fingerprint,
        fetched_reviews=len(reviews),
        min_reviews=min_reviews,
    )
    if action == "keep":
        return action
    if summary is None:
        summary = ReviewSummary(game_id=game.id, kind=kind, likes=[], dislikes=[])
        session.add(summary)
    if action == "not_enough":
        summary.status = "not_enough"
        summary.message = NOT_ENOUGH_MESSAGE
        summary.likes, summary.dislikes, summary.overview = [], [], None
        summary.review_count = len(reviews)
        summary.reviews_fingerprint = fingerprint
        summary.summary_generated_at = None
        return action
    if summary.status != "pending":  # a generation already in flight will pick up the new reviews
        summary.status = "outdated"
        summary.message = None
    return action
