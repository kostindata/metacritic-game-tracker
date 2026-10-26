"""Entry points for starting crawl runs (used by the web app and Celery tasks)."""

import time
from dataclasses import dataclass

from sqlalchemy import func, select, update

from app.config import get_settings
from app.db import session_scope
from app.logs import get_logger
from app.models import CrawlRun
from app.monitoring import CrawlLock, notify

log = get_logger(__name__)


@dataclass(frozen=True)
class TriggerResult:
    started: bool
    run_id: int | None
    lock_token: str | None = None
    message: str = ""


def active_run_id() -> int | None:
    with session_scope() as s:
        return s.scalar(
            select(CrawlRun.id)
            .where(CrawlRun.status.in_(("queued", "running")))
            .order_by(CrawlRun.id.desc())
            .limit(1)
        )


def close_orphaned_runs(lock: CrawlLock | None = None) -> int:
    """Close runs still marked active although their worker is gone (e.g. killed mid-run).

    Taking the lock first makes this race-free: while we hold it no run can be starting or
    running, so every active row is an orphan. If the lock is held, a run is alive (or its
    dead worker's lock has not expired yet) and nothing is touched. Returns rows closed.
    """
    lock = lock or CrawlLock()
    token = lock.acquire()
    if token is None:
        return 0
    try:
        with session_scope() as s:
            closed = s.execute(
                update(CrawlRun)
                .where(CrawlRun.status.in_(("queued", "running")))
                .values(status="aborted", finished_at=func.now(), current_game=None,
                        note="worker lost (lock expired)")
            ).rowcount
    finally:
        lock.release(token)
    if closed:
        log.warning("crawl.orphaned_runs_closed", count=closed)
        notify("run")
    return closed


def reserve_run(trigger: str, lock: CrawlLock | None = None) -> TriggerResult:
    """Take the crawl lock and create a queued run row, or report that one is active.

    The lock is taken *before* anything is enqueued, so two quick clicks (or a click
    racing the hourly schedule) can never produce two concurrent runs.
    """
    lock = lock or CrawlLock()
    token = lock.acquire()
    # The lock may be held for a few milliseconds by the orphaned-run check
    # (close_orphaned_runs) rather than by a run: retry briefly before saying "already running".
    for _ in range(10):
        if token is not None or active_run_id() is not None:
            break
        time.sleep(0.1)
        token = lock.acquire()
    if token is None:
        run_id = active_run_id()
        log.info("crawl.trigger_rejected", trigger=trigger, active_run=run_id)
        return TriggerResult(False, run_id, message="already running")
    try:
        with session_scope() as s:
            # We hold the lock, so any run still marked active belongs to a dead worker.
            s.execute(
                update(CrawlRun)
                .where(CrawlRun.status.in_(("queued", "running")))
                .values(status="aborted", finished_at=func.now(), note="worker lost (lock expired)")
            )
            run = CrawlRun(trigger=trigger, status="queued", target=get_settings().games_per_run)
            s.add(run)
            s.flush()
            run_id = run.id
    except Exception:
        lock.release(token)
        raise
    notify("run", run_id=run_id)
    log.info("crawl.run_reserved", trigger=trigger, run_id=run_id)
    return TriggerResult(True, run_id, token, message="started")


def trigger_manual_run() -> TriggerResult:
    from app.tasks import run_crawl  # local import: keeps web startup light

    result = reserve_run("manual")
    if not result.started:
        return result
    try:
        run_crawl.apply_async(args=[result.run_id, result.lock_token])
    except Exception as exc:
        CrawlLock().release(result.lock_token)
        with session_scope() as s:
            run = s.get(CrawlRun, result.run_id)
            run.status, run.note = "failed", f"could not enqueue: {exc}"
        raise
    return result
