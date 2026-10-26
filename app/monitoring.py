"""Redis-backed coordination: the crawl lock, worker heartbeats and change notifications."""

import json
import os
import socket
import threading
import time
import uuid
from functools import lru_cache
from typing import Any

import redis

from app.config import get_settings

LOCK_KEY = "mc:lock:crawl"
WORKER_PREFIX = "mc:worker:"
EVENTS_CHANNEL = "mc:events"
WORKER_TTL_SECONDS = 30
HEARTBEAT_SECONDS = 10


@lru_cache
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def notify(kind: str = "update", **payload: Any) -> None:
    """Wake up SSE streams; best effort."""
    try:
        get_redis().publish(EVENTS_CHANNEL, json.dumps({"kind": kind, **payload}, default=str))
    except redis.RedisError:
        pass


# --------------------------------------------------------------------------- crawl lock


class CrawlLock:
    """Single-holder lock with an owner token, TTL and refresh.

    The TTL protects against a crashed worker holding the lock forever; the running
    task refreshes it after every game.
    """

    _RELEASE = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"
    _REFRESH = (
        "if redis.call('GET', KEYS[1]) == ARGV[1] then "
        "return redis.call('EXPIRE', KEYS[1], ARGV[2]) end return 0"
    )

    def __init__(self, client: redis.Redis | None = None, ttl: int | None = None) -> None:
        self.redis = client or get_redis()
        self.ttl = ttl or get_settings().lock_ttl_seconds

    def acquire(self, token: str | None = None) -> str | None:
        token = token or uuid.uuid4().hex
        if self.redis.set(LOCK_KEY, token, nx=True, ex=self.ttl):
            return token
        return None

    def holds(self, token: str) -> bool:
        return self.redis.get(LOCK_KEY) == token

    def refresh(self, token: str) -> bool:
        return bool(self.redis.eval(self._REFRESH, 1, LOCK_KEY, token, self.ttl))

    def release(self, token: str) -> bool:
        return bool(self.redis.eval(self._RELEASE, 1, LOCK_KEY, token))

    def is_locked(self) -> bool:
        return bool(self.redis.exists(LOCK_KEY))


class LockRefreshingLimiter:
    """Rate limiter wrapper that also extends the crawl lock before each request.

    Each request is bounded (timeout + backoff), so a live run keeps its lock with a
    short TTL, while the lock of a killed worker expires within `lock_ttl_seconds`.
    """

    def __init__(self, inner, lock: CrawlLock, token: str) -> None:
        self.inner, self.lock, self.token = inner, lock, token

    def wait(self) -> None:
        self.lock.refresh(self.token)
        self.inner.wait()


# --------------------------------------------------------------------------- graceful stop

STOP_PREFIX = "mc:stop:"


def _stop_key(host: str | None) -> str:
    return STOP_PREFIX + (host or socket.gethostname())


def request_stop(host: str | None = None, client: redis.Redis | None = None) -> None:
    """Ask runs on this host to stop after the current game (set on worker shutdown).

    Celery's main process gets SIGTERM, but the child running the crawl does not, so the
    flag goes through Redis; main and child share the container hostname.
    """
    try:
        (client or get_redis()).set(_stop_key(host), "1", ex=600)
    except redis.RedisError:
        pass


def clear_stop(host: str | None = None, client: redis.Redis | None = None) -> None:
    try:
        (client or get_redis()).delete(_stop_key(host))
    except redis.RedisError:
        pass


def stop_requested(host: str | None = None, client: redis.Redis | None = None) -> bool:
    try:
        return bool((client or get_redis()).exists(_stop_key(host)))
    except redis.RedisError:
        return False


# --------------------------------------------------------------------------- worker status


class WorkerStatus:
    """Heartbeat + status (idle / busy / error) for one worker process."""

    def __init__(self, name: str | None = None, client: redis.Redis | None = None) -> None:
        settings = get_settings()
        self.id = name or f"{settings.worker_name}@{socket.gethostname()}/{os.getpid()}"
        self.redis = client or get_redis()
        self.state: dict[str, Any] = {
            "id": self.id,
            "name": settings.worker_name,
            "status": "idle",
            "task": None,
            "detail": None,
            "error": None,
            "since": time.time(),
        }
        self._lock = threading.Lock()
        self._stop = threading.Event()

    @property
    def key(self) -> str:
        return WORKER_PREFIX + self.id

    def _write(self) -> None:
        with self._lock:
            state = dict(self.state, heartbeat=time.time())
        try:
            self.redis.set(self.key, json.dumps(state), ex=WORKER_TTL_SECONDS)
        except redis.RedisError:
            pass

    def update(self, **fields: Any) -> None:
        with self._lock:
            if "status" in fields and fields["status"] != self.state.get("status"):
                fields.setdefault("since", time.time())
            self.state.update(fields)
        self._write()
        notify("worker")

    def start_heartbeat(self) -> None:
        self._write()

        def loop() -> None:
            while not self._stop.wait(HEARTBEAT_SECONDS):
                self._write()

        threading.Thread(target=loop, name="worker-heartbeat", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.redis.delete(self.key)
        except redis.RedisError:
            pass


_current: WorkerStatus | None = None


def current_worker() -> WorkerStatus | None:
    return _current


def set_current_worker(status: WorkerStatus | None) -> None:
    global _current
    _current = status


def set_task_detail(detail: str) -> None:
    if _current is not None:
        _current.update(detail=detail)


def list_workers(client: redis.Redis | None = None) -> list[dict[str, Any]]:
    r = client or get_redis()
    keys = sorted(r.scan_iter(match=WORKER_PREFIX + "*", count=100))
    workers = []
    for raw in r.mget(keys) if keys else []:
        if raw:
            try:
                workers.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return sorted(workers, key=lambda w: (w.get("name") or "", w.get("id") or ""))
