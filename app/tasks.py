from datetime import datetime, timezone

from app import covers
from app.config import get_settings
from app.crawler.runner import CrawlRunner, apply_daily_reset
from app.crawler.service import close_orphaned_runs, reserve_run
from app.db import get_sessionmaker, session_scope
from app.llm.summarizer import OpenRouterClient, generate_summary
from app.similarity import refresh_similar
from app.logs import get_logger
from app.models import CrawlRun
from app.monitoring import CrawlLock, LockRefreshingLimiter, get_redis, notify, stop_requested
from app.scraper.client import MetacriticClient, RedisRateLimiter
from app.worker import celery_app

log = get_logger(__name__)


def _execute_run(run_id: int, lock_token: str) -> dict:
    settings = get_settings()
    lock = CrawlLock()
    if not lock.holds(lock_token):
        with session_scope() as s:
            run = s.get(CrawlRun, run_id)
            if run and run.status == "queued":
                run.status, run.note = "aborted", "lock expired before the run started"
                run.finished_at = datetime.now(timezone.utc)
        notify("run", run_id=run_id)
        return {"run_id": run_id, "status": "aborted"}

    limiter = LockRefreshingLimiter(RedisRateLimiter(get_redis(), settings.request_interval_seconds), lock, lock_token)
    client = MetacriticClient(settings, limiter)
    try:
        runner = CrawlRunner(
            settings=settings,
            client=client,
            session_factory=get_sessionmaker(),
            lock=lock,
            lock_token=lock_token,
            should_stop=stop_requested,
        )
        run = runner.run(run_id)
        return {"run_id": run_id, "status": run.status, "processed": run.processed, "failed": run.failed,
                "requests": client.request_count}
    finally:
        client.close()
        lock.release(lock_token)
        notify("run", run_id=run_id)
        # New and updated games change who is similar to whom.
        refresh_similar_games.apply_async()
        # Covers whose download failed or was interrupted (e.g. by a restart).
        backfill_covers.apply_async()


@celery_app.task(name="app.tasks.run_crawl")
def run_crawl(run_id: int, lock_token: str) -> dict:
    """A run reserved by the web app (manual trigger)."""
    return _execute_run(run_id, lock_token)


@celery_app.task(name="app.tasks.scheduled_crawl")
def scheduled_crawl() -> dict:
    result = reserve_run("scheduled")
    if not result.started:
        log.info("crawl.scheduled_skipped", reason="already running", active_run=result.run_id)
        return {"status": "skipped", "active_run": result.run_id}
    return _execute_run(result.run_id, result.lock_token)


@celery_app.task(name="app.tasks.daily_reset")
def daily_reset() -> dict:
    """Local midnight: clear "processed today" and send the cursor back to New Releases.

    Idempotent; runs also perform the same check lazily on start, so a missed or late
    reset never lets yesterday's cursor leak into today.
    """
    settings = get_settings()
    with session_scope() as s:
        cursor, reset = apply_daily_reset(s, datetime.now(timezone.utc), settings)
    notify("reset")
    return {"day": str(cursor.day), "reset": reset}


@celery_app.task(name="app.tasks.summarize_game")
def summarize_game(game_id: int, kind: str) -> str:
    settings = get_settings()
    llm = OpenRouterClient(settings)
    try:
        status = generate_summary(get_sessionmaker(), llm, game_id, kind)
    finally:
        llm.http.close()
    notify("llm", game_id=game_id)
    return status



@celery_app.task(name="app.tasks.cleanup_runs")
def cleanup_runs() -> dict:
    """Every minute: close runs whose worker was killed without a clean shutdown."""
    return {"closed": close_orphaned_runs()}


@celery_app.task(name="app.tasks.refresh_similar_games")
def refresh_similar_games() -> dict:
    """Recompute precomputed similar games for the whole database (after each crawl run)."""
    with session_scope() as s:
        games = refresh_similar(s)
    return {"games": games}


COVERS_PER_BACKFILL = 300  # about 5 minutes at one request per second
COVERS_LOCK_KEY = "mc:lock:covers"


@celery_app.task(name="app.tasks.backfill_covers")
def backfill_covers() -> dict:
    """Download missing cover images (after each crawl run). One at a time: the lock expires on
    its own if the worker dies mid-way."""
    redis = get_redis()
    if not redis.set(COVERS_LOCK_KEY, "1", nx=True, ex=30 * 60):
        return {"status": "skipped", "reason": "already running"}
    try:
        stored, failed = covers.backfill(limit=COVERS_PER_BACKFILL)
    finally:
        redis.delete(COVERS_LOCK_KEY)
    return {"stored": stored, "failed": failed}
