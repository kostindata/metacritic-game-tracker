"""One crawl run: walk the listing from the persistent cursor and process up to N new games."""

from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import delete, exists, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.crawler.cursor import Cursor, OrderedProgress, WalkResult, crawl_day, ensure_current_day, walk_listing
from app.crawler.upsert import KINDS, plan_summary, replace_reviews, upsert_game
from app.logs import get_logger
from app.models import CrawlError, CrawlRun, CrawlState, NewReleaseEntry, ProcessedToday
from app.monitoring import CrawlLock, notify, set_task_detail
from app.scraper.client import MetacriticClient
from app.scraper.parsers import GameDetail, ListingItem, ReviewData

log = get_logger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LockLostError(RuntimeError):
    pass


class WorkerStoppingError(RuntimeError):
    pass


# --------------------------------------------------------------------------- state repository


def load_cursor(session: Session) -> Cursor | None:
    state = session.get(CrawlState, 1)
    if state is None:
        return None
    return Cursor(day=state.day, phase=state.phase, page=state.page, position=state.position)


def save_cursor(session: Session, cursor: Cursor) -> None:
    state = session.get(CrawlState, 1)
    if state is None:
        session.add(CrawlState(id=1, day=cursor.day, phase=cursor.phase, page=cursor.page, position=cursor.position))
    else:
        state.day, state.phase, state.page, state.position = cursor.day, cursor.phase, cursor.page, cursor.position


def apply_daily_reset(session: Session, now: datetime, settings: Settings) -> tuple[Cursor, bool]:
    """Roll the cursor and the processed-today set over to the current crawl day if needed."""
    today = crawl_day(now, settings.tz, settings.daily_reset_hour)
    cursor, reset = ensure_current_day(load_cursor(session), today)
    if reset:
        session.execute(delete(ProcessedToday).where(ProcessedToday.day < today))
        save_cursor(session, cursor)
        log.info("crawl.daily_reset", day=str(today))
    return cursor, reset


def save_new_releases(session: Session, day, items: list[ListingItem]) -> bool:
    """Store the day's New Releases list; only the first fetch of a day is kept."""
    if session.scalar(select(exists().where(NewReleaseEntry.day == day))):
        return False
    session.add_all(
        NewReleaseEntry(day=day, position=i, slug=item.slug, title=item.title) for i, item in enumerate(items)
    )
    return True


def mark_processed(session: Session, day, slug: str, outcome: str, run_id: int, now: datetime) -> None:
    stmt = insert(ProcessedToday).values(day=day, slug=slug, outcome=outcome, run_id=run_id, processed_at=now)
    session.execute(stmt.on_conflict_do_nothing(index_elements=[ProcessedToday.day, ProcessedToday.slug]))


def record_error(session: Session, run_id: int | None, slug: str | None, stage: str, message: str) -> None:
    session.add(CrawlError(run_id=run_id, game_slug=slug, stage=stage, message=message[:2000]))


# --------------------------------------------------------------------------- per-game work


@dataclass
class FetchedGame:
    detail: GameDetail
    reviews: dict[str, list[ReviewData] | None] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (stage, message)


def fetch_game(client: MetacriticClient, slug: str, max_reviews: int) -> FetchedGame:
    """All network work for one game. Raises only if the game page itself fails."""
    detail = client.game(slug)
    fetched = FetchedGame(detail=detail)

    for p in detail.platforms:
        if p.user_stats_on_page:
            continue  # already on the game page (also when it is "no ratings yet")
        if not p.slug:
            continue
        try:
            stats = client.user_stats(detail.slug, p.slug)
            p.user_score, p.user_review_count = stats.score, stats.review_count
        except Exception as exc:  # noqa: BLE001 - isolate per platform
            p.user_stats_failed = True
            fetched.errors.append(("user_stats", f"{p.name}: {exc}"))

    for kind in KINDS:
        count_attr = "critic_review_count" if kind == "critic" else "user_review_count"
        platforms = sorted(
            (p for p in detail.platforms if p.slug and getattr(p, count_attr) > 0),
            key=lambda p: getattr(p, count_attr),
            reverse=True,
        )
        collected: list[ReviewData] = []
        try:
            for p in platforms:
                offset = 0
                while len(collected) < max_reviews:
                    page = client.reviews(kind, detail.slug, p.slug, offset, max_reviews - len(collected))
                    if not page.reviews:
                        break
                    collected.extend(page.reviews[: max_reviews - len(collected)])
                    offset += len(page.reviews)
                    if offset >= page.total:
                        break
                if len(collected) >= max_reviews:
                    break
            fetched.reviews[kind] = collected
        except Exception as exc:  # noqa: BLE001 - keep game data even if reviews fail
            fetched.reviews[kind] = None
            fetched.errors.append((f"{kind}_reviews", str(exc)))
    return fetched


# --------------------------------------------------------------------------- run


class CrawlRunner:
    def __init__(
        self,
        settings: Settings,
        client: MetacriticClient,
        session_factory: sessionmaker[Session],
        lock: CrawlLock,
        lock_token: str,
        now: Callable[[], datetime] = utcnow,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self.settings = settings
        self.client = client
        self.sessions = session_factory
        self.lock = lock
        self.token = lock_token
        self.now = now
        self.should_stop = should_stop

    def _update_run(self, run_id: int, **fields) -> None:
        with self.sessions.begin() as s:
            s.execute(update(CrawlRun).where(CrawlRun.id == run_id).values(**fields))
        notify("run", run_id=run_id)

    def run(self, run_id: int) -> CrawlRun:
        target = self.settings.games_per_run
        with self.sessions.begin() as s:
            # Anything else still marked active is a leftover of a crashed worker.
            s.execute(
                update(CrawlRun)
                .where(CrawlRun.id != run_id, CrawlRun.status.in_(("queued", "running")))
                .values(status="aborted", finished_at=self.now(), note="superseded (worker crashed?)")
            )
            run = s.get(CrawlRun, run_id)
            if run is None:
                raise ValueError(f"crawl run {run_id} not found")
            run.status, run.started_at, run.target = "running", self.now(), target
            cursor, _ = apply_daily_reset(s, self.now(), self.settings)
        notify("run", run_id=run_id)
        log.info("crawl.run_started", run_id=run_id, cursor=cursor.describe())

        counts = dict(processed=0, inserted=0, updated=0, failed=0, skipped=0)
        walk = WalkResult()
        status, note = "success", None
        progress = OrderedProgress(cursor)
        concurrency = max(1, self.settings.crawl_concurrency)
        inflight: dict[Future, tuple[int, str]] = {}  # future -> (listing seq, label)
        submitted = 0

        def is_processed(slug: str) -> bool:
            with self.sessions() as s:
                return s.get(ProcessedToday, (cursor.day, slug)) is not None

        def save_progress() -> None:
            with self.sessions.begin() as s:
                save_cursor(s, progress.cursor)
                s.execute(
                    update(CrawlRun).where(CrawlRun.id == run_id)
                    .values(current_game=", ".join(label for _, label in inflight.values())[:500] or None, **counts)
                )
            done = counts["processed"] + counts["failed"]
            set_task_detail(f"{done}/{target} · в работе: {len(inflight)}")
            notify("run", run_id=run_id)

        def collect(block: bool) -> None:
            """Account for finished games; with block=True wait until at least one finishes."""
            if not inflight:
                return
            finished, _ = wait(list(inflight), timeout=None if block else 0, return_when=FIRST_COMPLETED)
            for future in finished:
                seq, _label = inflight.pop(future)
                try:
                    outcome = future.result()
                except Exception:  # noqa: BLE001 - _process_one isolates errors; belt and braces
                    log.exception("crawl.game_crashed", run_id=run_id)
                    outcome = "failed"
                if outcome == "failed":
                    counts["failed"] += 1
                else:
                    counts["processed"] += 1
                    counts[outcome] += 1
                progress.complete(seq)
            if finished:
                save_progress()

        pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="crawl-game")
        try:
            steps = walk_listing(
                cursor,
                fetch_new_releases=lambda: self._fetch_new_releases(cursor.day, self.settings.new_releases_count),
                fetch_browse_page=self.client.browse_page,
                is_processed=is_processed,
                max_pages=self.settings.max_listing_pages_per_run,
                result=walk,
            )
            # The listing is walked in order on this thread; up to `concurrency` games are fetched
            # and stored in parallel. All Metacritic requests still share the global rate limiter.
            for step in steps:
                if self.should_stop():
                    raise WorkerStoppingError("worker stopped")
                if not self.lock.refresh(self.token):
                    raise LockLostError("crawl lock lost")
                seq = progress.add(step.cursor_after)
                if step.already_processed:
                    counts["skipped"] += 1
                    if progress.complete(seq) and not inflight:
                        save_progress()
                    continue
                while len(inflight) >= concurrency:
                    collect(block=True)
                label = step.item.title or step.item.slug
                inflight[pool.submit(self._process_one, run_id, cursor.day, step.item.slug)] = (seq, label)
                submitted += 1
                collect(block=False)
                save_progress()
                if submitted >= target:
                    break
            if walk.exhausted:
                note = "listing exhausted"
        except (LockLostError, WorkerStoppingError) as exc:
            # Games already started are finished below; the cursor never passes an unfinished one.
            status, note = "aborted", str(exc)
        except Exception as exc:  # noqa: BLE001 - listing failure ends the run, cursor is saved
            status, note = "failed", f"{type(exc).__name__}: {exc}"
            log.exception("crawl.run_failed", run_id=run_id)
            with self.sessions.begin() as s:
                record_error(s, run_id, None, "listing", note)
        finally:
            while inflight:
                collect(block=True)
            pool.shutdown(wait=True)

        if status == "success" and counts["failed"]:
            status = "partial"
        with self.sessions.begin() as s:
            s.execute(
                update(CrawlRun)
                .where(CrawlRun.id == run_id)
                .values(status=status, note=note, finished_at=self.now(), current_game=None, **counts)
            )
            run = s.get(CrawlRun, run_id)
        notify("run", run_id=run_id)
        log.info("crawl.run_finished", run_id=run_id, status=status, note=note, **counts)
        return run

    def _fetch_new_releases(self, day, limit: int) -> list[ListingItem]:
        items = self.client.new_releases(limit=limit)
        with self.sessions.begin() as s:
            save_new_releases(s, day, items)
        return items

    def _process_one(self, run_id: int, day, slug: str) -> str:
        """Fetch and store one game (runs on a pool thread). Returns inserted|updated|failed."""
        log.info("crawl.game_started", run_id=run_id, slug=slug)
        try:
            fetched = fetch_game(self.client, slug, self.settings.max_reviews_per_type)
        except Exception as exc:  # noqa: BLE001 - per-game isolation
            log.warning("crawl.game_failed", run_id=run_id, slug=slug, error=str(exc))
            with self.sessions.begin() as s:
                record_error(s, run_id, slug, "game", f"{type(exc).__name__}: {exc}")
            return "failed"

        outdated: list[str] = []
        try:
            with self.sessions.begin() as s:
                game, inserted = upsert_game(s, fetched.detail, self.now())
                for kind in KINDS:
                    reviews = fetched.reviews.get(kind)
                    if reviews is None:
                        continue  # fetch failed: keep previous reviews and summary
                    replace_reviews(s, game.id, kind, reviews)
                    if plan_summary(s, game, kind, reviews, self.settings.min_reviews_for_summary) == "regenerate":
                        outdated.append(kind)
                for stage, message in fetched.errors:
                    record_error(s, run_id, slug, stage, message)
                outcome = "inserted" if inserted else "updated"
                mark_processed(s, day, fetched.detail.slug, outcome, run_id, self.now())
                if fetched.detail.slug != slug:
                    mark_processed(s, day, slug, "updated", run_id, self.now())
        except Exception as exc:  # noqa: BLE001
            log.exception("crawl.game_store_failed", run_id=run_id, slug=slug)
            with self.sessions.begin() as s:
                record_error(s, run_id, slug, "store", f"{type(exc).__name__}: {exc}")
            return "failed"

        log.info("crawl.game_done", run_id=run_id, slug=slug, inserted=inserted,
                 platforms=len(fetched.detail.platforms), summaries_outdated=outdated)
        return outcome
