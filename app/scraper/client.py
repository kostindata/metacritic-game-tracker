"""Polite Metacritic HTTP client.

* A process-shared rate limiter (Redis) guarantees <= 1 request/second across all workers.
* Transient failures (network errors, 429, 5xx) are retried with exponential backoff + jitter.
* Pages are fetched with plain httpx; if the server-rendered payload is missing (e.g. the
  page was served as a JS shell) we fall back to rendering it with headless Chromium.
"""

import random
import threading
import time
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.logs import get_logger
from app.scraper import parsers

log = get_logger(__name__)

HOME_URL = "https://www.metacritic.com/game/"
BROWSE_URL = "https://www.metacritic.com/browse/game/all/all/all-time/new/"
BACKEND = "https://backend.metacritic.com/reviews/metacritic"

_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    pass


class NotFoundError(FetchError):
    pass


class RateLimiter(Protocol):
    def wait(self) -> None: ...


class LocalRateLimiter:
    """In-process limiter; used in tests and as a fallback."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:  # reserve a slot atomically; sleep outside the lock
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            time.sleep(delay)


class RedisRateLimiter:
    """Global limiter: atomically reserves the next free slot in Redis."""

    _SCRIPT = """
    local now = tonumber(ARGV[1])
    local interval = tonumber(ARGV[2])
    local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
    local slot = math.max(now, nxt)
    redis.call('SET', KEYS[1], tostring(slot + interval), 'PX', math.ceil(interval * 1000) + 60000)
    return tostring(slot - now)
    """

    def __init__(self, redis_client, interval: float, key: str = "mc:ratelimit:metacritic") -> None:
        self.interval = interval
        self.key = key
        self._script = redis_client.register_script(self._SCRIPT)
        self._redis = redis_client

    def wait(self) -> None:
        # Use the Redis server clock so all workers agree on "now".
        sec, usec = self._redis.time()
        delay = float(self._script(keys=[self.key], args=[sec + usec / 1e6, self.interval]))
        if delay > 0:
            time.sleep(delay)


def _render_with_browser(url: str, user_agent: str, timeout_s: float) -> str:
    from playwright.sync_api import sync_playwright  # heavy import, only when needed

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=user_agent)
            page.goto(url, wait_until="networkidle", timeout=timeout_s * 1000)
            return page.content()
        finally:
            browser.close()


class MetacriticClient:
    def __init__(self, settings: Settings, limiter: RateLimiter, http: httpx.Client | None = None) -> None:
        self.settings = settings
        self.limiter = limiter
        self.http = http or httpx.Client(
            timeout=settings.http_timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        self.request_count = 0
        self._count_lock = threading.Lock()

    def _count_request(self) -> None:
        with self._count_lock:  # the client is shared by the concurrent game workers
            self.request_count += 1

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------------ transport

    def _request(self, url: str, params: dict[str, Any] | None = None, accept: str = "text/html") -> httpx.Response:
        attempts = self.settings.http_max_attempts
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            self.limiter.wait()
            self._count_request()
            started = time.monotonic()
            try:
                resp = self.http.get(url, params=params, headers={"Accept": accept})
            except httpx.TransportError as exc:
                last_error = exc
                log.warning("http.transport_error", url=url, attempt=attempt, error=str(exc))
                retry_after = None
            else:
                log.debug("http.response", url=str(resp.url), status=resp.status_code,
                          ms=int((time.monotonic() - started) * 1000))
                if resp.status_code == 404:
                    raise NotFoundError(f"404 for {url}")
                if resp.status_code < 400:
                    return resp
                last_error = FetchError(f"HTTP {resp.status_code} for {url}")
                if resp.status_code not in _RETRY_STATUS:
                    raise last_error
                retry_after = _retry_after(resp)
                log.warning("http.retryable_status", url=url, status=resp.status_code, attempt=attempt)
            if attempt < attempts:
                backoff = self.settings.http_backoff_base_seconds * 2 ** (attempt - 1)
                time.sleep(max(retry_after or 0, backoff) + random.uniform(0, 0.5))
        raise FetchError(f"giving up on {url} after {attempts} attempts: {last_error}")

    def get_html(self, url: str, params: dict[str, Any] | None = None) -> str:
        text = self._request(url, params=params).text
        if "__NUXT_DATA__" in text or not self.settings.playwright_enabled:
            return text
        log.info("http.render_with_browser", url=url)
        full_url = str(httpx.URL(url, params=params)) if params else url
        self.limiter.wait()
        self._count_request()
        return _render_with_browser(full_url, self.settings.user_agent, self.settings.http_timeout_seconds)

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._request(url, params=params, accept="application/json")
        try:
            return resp.json()
        except ValueError as exc:
            raise FetchError(f"invalid JSON from {url}") from exc

    # ------------------------------------------------------------------ endpoints

    def new_releases(self, limit: int = 20) -> list[parsers.ListingItem]:
        return parsers.parse_new_releases(self.get_html(HOME_URL), limit=limit)

    def browse_page(self, page: int) -> list[parsers.ListingItem]:
        return parsers.parse_browse_page(self.get_html(BROWSE_URL, params={"page": page}))

    def game(self, slug: str) -> parsers.GameDetail:
        return parsers.parse_game_page(self.get_html(parsers.game_url(slug)), slug)

    def user_stats(self, slug: str, platform_slug: str) -> parsers.ScoreStats:
        url = f"{BACKEND}/user/games/{slug}/platform/{platform_slug}/stats/web"
        return parsers.parse_score_stats(self.get_json(url))

    def reviews(self, kind: str, slug: str, platform_slug: str, offset: int, limit: int) -> parsers.ReviewPage:
        url = f"{BACKEND}/{kind}/games/{slug}/platform/{platform_slug}/web"
        params = {"offset": offset, "limit": limit, "filterBySentiment": "all", "sort": "date"}
        return parsers.parse_reviews(self.get_json(url, params=params), kind)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        # Capped so the gap between lock refreshes stays well below the lock TTL.
        return min(float(value), 60.0)
    except ValueError:
        return None
