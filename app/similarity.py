"""Similar-games scoring (formula documented in README.md).

    similarity = 0.50 * jaccard(genres)
               + 0.20 * jaccard(developers)
               + 0.20 * jaccard(platforms)
               + 0.10 * date_closeness

    date_closeness = max(0, 1 - |days between releases| / 730)   (0 if a date is unknown)
"""

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete, func, insert, or_, select
from sqlalchemy.orm import Session, selectinload

from app.logs import get_logger
from app.models import Game, GamePlatform, GameSimilar

log = get_logger(__name__)

W_GENRES = 0.50
W_DEVELOPERS = 0.20
W_PLATFORMS = 0.20
W_RELEASE = 0.10
RELEASE_WINDOW_DAYS = 730
CANDIDATE_LIMIT = 1000


@dataclass(frozen=True)
class Features:
    genres: frozenset[str]
    developers: frozenset[str]
    platforms: frozenset[str]
    release_date: date | None

    @classmethod
    def of(cls, genres, developers, platforms, release_date) -> "Features":
        norm = lambda xs: frozenset(x.strip().lower() for x in xs or [] if x and x.strip())  # noqa: E731
        return cls(norm(genres), norm(developers), norm(platforms), release_date)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def date_closeness(a: date | None, b: date | None) -> float:
    if a is None or b is None:
        return 0.0
    return max(0.0, 1.0 - abs((a - b).days) / RELEASE_WINDOW_DAYS)


def similarity(a: Features, b: Features) -> float:
    return (
        W_GENRES * jaccard(a.genres, b.genres)
        + W_DEVELOPERS * jaccard(a.developers, b.developers)
        + W_PLATFORMS * jaccard(a.platforms, b.platforms)
        + W_RELEASE * date_closeness(a.release_date, b.release_date)
    )


def rank_similar(target: Features, candidates: list[tuple[object, Features]], limit: int = 6) -> list[tuple[object, float]]:
    """Top `limit` candidates by score. Needs a shared genre, developer or platform —
    a release date alone is not enough to call two games similar."""
    scored = []
    for key, feats in candidates:
        if not (target.genres & feats.genres or target.developers & feats.developers
                or target.platforms & feats.platforms):
            continue
        scored.append((key, similarity(target, feats)))
    scored.sort(key=lambda kv: kv[1], reverse=True)  # stable: ties keep candidate order
    return scored[:limit]


def _features(game: Game) -> Features:
    return Features.of(game.genres, game.developers, [p.platform for p in game.platforms], game.release_date)


def similar_games(session: Session, game: Game, limit: int = 6) -> list[tuple[Game, float]]:
    platforms = [p.platform for p in game.platforms]
    conditions = []
    if game.genres:
        conditions.append(Game.genres.overlap(game.genres))
    if game.developers:
        conditions.append(Game.developers.overlap(game.developers))
    if platforms:
        conditions.append(Game.platforms.any(GamePlatform.platform.in_(platforms)))
    if not conditions:
        return []
    # Pre-filter in SQL, prefer genre matches when the candidate pool is large.
    stmt = (
        select(Game)
        .where(Game.id != game.id, or_(*conditions))
        .options(selectinload(Game.platforms))
        .order_by(Game.genres.overlap(game.genres or ["\x00"]).desc(), Game.last_crawled_at.desc())
        .limit(CANDIDATE_LIMIT)
    )
    candidates = [(g, _features(g)) for g in session.scalars(stmt)]
    return rank_similar(_features(game), candidates, limit)  # type: ignore[return-value]


# --------------------------------------------------------------------------- precomputation

SIMILAR_REFRESH_LOCK = 0x5153_494D  # arbitrary advisory-lock key for refresh_similar
PLATFORM_FALLBACK = 300  # recent same-platform games considered when few close matches exist


def compute_all_similar(
    games: dict[int, Features], limit: int = 6, platform_fallback: int = PLATFORM_FALLBACK
) -> dict[int, list[tuple[int, float]]]:
    """Top `limit` similar games for every game, without comparing all pairs.

    Candidates are the games sharing a genre or a developer (inverted index). When that gives
    too few, the most recent `platform_fallback` games sharing a platform are added, so games
    with rare genres still get suggestions while popular platforms (PC: most of the DB) do not
    make it quadratic.
    """
    by_genre: dict[str, list[int]] = defaultdict(list)
    by_dev: dict[str, list[int]] = defaultdict(list)
    by_platform: dict[str, list[int]] = defaultdict(list)
    # Newest first, so platform fallbacks prefer recent games; ids break ties deterministically.
    order = sorted(games, key=lambda i: (games[i].release_date or date.min, i), reverse=True)
    for gid in order:
        f = games[gid]
        for g in f.genres:
            by_genre[g].append(gid)
        for d in f.developers:
            by_dev[d].append(gid)
        for p in f.platforms:
            by_platform[p].append(gid)

    result: dict[int, list[tuple[int, float]]] = {}
    for gid, f in games.items():
        candidates: dict[int, None] = {}
        for g in f.genres:
            candidates.update(dict.fromkeys(by_genre[g]))
        for d in f.developers:
            candidates.update(dict.fromkeys(by_dev[d]))
        candidates.pop(gid, None)
        if len(candidates) < limit * 3:
            for p in f.platforms:
                candidates.update(dict.fromkeys(by_platform[p][:platform_fallback]))
            candidates.pop(gid, None)
        ranked = rank_similar(f, [(cid, games[cid]) for cid in candidates], limit=len(candidates))
        ranked.sort(key=lambda kv: (-round(kv[1], 9), kv[0]))  # score, then id: deterministic
        result[gid] = ranked[:limit]
    return result


def refresh_similar(session: Session, limit: int = 6) -> int:
    """Recompute the game_similar table for the whole database. Returns games covered."""
    started = time.monotonic()
    # Two refreshes overlapping (two crawl worker processes) would both delete and re-insert
    # and one would fail on the primary key; serialise them for the transaction instead.
    session.execute(select(func.pg_advisory_xact_lock(SIMILAR_REFRESH_LOCK)))
    platforms = (
        select(GamePlatform.game_id, func.array_agg(GamePlatform.platform).label("platforms"))
        .group_by(GamePlatform.game_id)
        .subquery()
    )
    rows = session.execute(
        select(Game.id, Game.genres, Game.developers, Game.release_date, platforms.c.platforms)
        .outerjoin(platforms, platforms.c.game_id == Game.id)
    ).all()
    games = {r.id: Features.of(r.genres, r.developers, r.platforms or [], r.release_date) for r in rows}
    similar = compute_all_similar(games, limit=limit)
    session.execute(delete(GameSimilar))
    values = [
        {"game_id": gid, "rank": rank, "similar_id": sid, "score": score}
        for gid, items in similar.items()
        for rank, (sid, score) in enumerate(items)
    ]
    for i in range(0, len(values), 5000):
        session.execute(insert(GameSimilar), values[i : i + 5000])
    log.info("similar.refreshed", games=len(games), rows=len(values), ms=int((time.monotonic() - started) * 1000))
    return len(games)
