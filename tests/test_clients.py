import json

import httpx
import pytest

from app.config import Settings
from app.llm.summarizer import OpenRouterClient, SummaryError
from app.scraper.client import FetchError, LocalRateLimiter, MetacriticClient, NotFoundError


class CountingLimiter:
    def __init__(self):
        self.calls = 0

    def wait(self):
        self.calls += 1


def make_client(handler, **overrides):
    settings = Settings(http_backoff_base_seconds=0, playwright_enabled=False, **overrides)
    limiter = CountingLimiter()
    http = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": settings.user_agent})
    return MetacriticClient(settings, limiter, http=http), limiter


class TestMetacriticClient:
    def test_retries_transient_errors_then_succeeds(self):
        statuses = iter([503, 429, 200])

        def handler(request):
            return httpx.Response(next(statuses), json={"data": {"item": {"score": 7.5, "reviewCount": 4}}})

        client, limiter = make_client(handler)
        stats = client.user_stats("game", "pc")
        assert stats.score == 7.5
        assert limiter.calls == 3  # every attempt goes through the rate limiter

    def test_transport_errors_are_retried(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(200, json={"data": {"item": {"score": 0, "reviewCount": 0}}})

        client, _ = make_client(handler)
        assert client.user_stats("game", "pc").score is None
        assert calls["n"] == 2

    def test_gives_up_after_max_attempts(self):
        client, limiter = make_client(lambda r: httpx.Response(500), http_max_attempts=3)
        with pytest.raises(FetchError):
            client.user_stats("game", "pc")
        assert limiter.calls == 3

    def test_404_is_not_retried(self):
        client, limiter = make_client(lambda r: httpx.Response(404))
        with pytest.raises(NotFoundError):
            client.game("missing")
        assert limiter.calls == 1

    def test_sends_realistic_user_agent_and_review_params(self):
        seen = {}

        def handler(request):
            seen["ua"] = request.headers["User-Agent"]
            seen["url"] = request.url
            return httpx.Response(200, json={"data": {"totalResults": 0, "items": []}})

        client, _ = make_client(handler)
        client.reviews("critic", "elden-ring", "pc", offset=10, limit=20)
        assert seen["ua"].startswith("Mozilla/5.0")
        assert seen["url"].path == "/reviews/metacritic/critic/games/elden-ring/platform/pc/web"
        assert seen["url"].params["offset"] == "10" and seen["url"].params["limit"] == "20"


def test_lock_refreshing_limiter_extends_lock_before_each_request():
    from app.monitoring import LockRefreshingLimiter

    events = []

    class FakeLock:
        def refresh(self, token):
            events.append(("refresh", token))
            return True

    class Inner:
        def wait(self):
            events.append(("wait",))

    limiter = LockRefreshingLimiter(Inner(), FakeLock(), "tok")
    limiter.wait()
    limiter.wait()
    assert events == [("refresh", "tok"), ("wait",)] * 2


def test_local_rate_limiter_spaces_calls(monkeypatch):
    clock = {"t": 100.0}
    sleeps = []
    monkeypatch.setattr("app.scraper.client.time.monotonic", lambda: clock["t"])
    monkeypatch.setattr("app.scraper.client.time.sleep", lambda s: sleeps.append(s))
    limiter = LocalRateLimiter(1.0)
    limiter.wait()
    limiter.wait()
    clock["t"] += 0.25
    limiter.wait()
    assert sleeps == [pytest.approx(1.0), pytest.approx(1.75)]


class _Review:
    def __init__(self, quote, score=8.0, platform="PC"):
        self.quote, self.score, self.platform = quote, score, platform


class TestOpenRouterClient:
    def _client(self, handler, key="test-key"):
        settings = Settings(openrouter_api_key=key, llm_max_attempts=2)
        return OpenRouterClient(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))

    def test_success(self, monkeypatch):
        monkeypatch.setattr("app.llm.summarizer.time.sleep", lambda s: None)
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            captured["auth"] = request.headers["Authorization"]
            content = json.dumps({"overview": "Overall consensus. " * 10,
                                  "likes": ["Great gunplay", "Strong campaign", "Pretty"], "dislikes": ["Short", "Bugs", "Price"]})
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        summary = self._client(handler).summarise("Game", "critic", [_Review("Fun game"), _Review("Too short", 6)])
        assert summary.likes[0] == "Great gunplay" and len(summary.dislikes) == 3
        assert captured["auth"] == "Bearer test-key"
        assert captured["body"]["model"] == "deepseek/deepseek-v4.1-flash"
        assert captured["body"]["reasoning"] == {"enabled": False}  # faster summaries by default
        prompt = captured["body"]["messages"][1]["content"]
        assert "Fun game" in prompt and "Too short" in prompt and "professional critics" in prompt
        assert "Write everything in Russian" in captured["body"]["messages"][0]["content"]

    def test_retries_bad_output_then_fails(self, monkeypatch):
        monkeypatch.setattr("app.llm.summarizer.time.sleep", lambda s: None)
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json={"choices": [{"message": {"content": "sorry, no"}}]})

        with pytest.raises(SummaryError):
            self._client(handler).summarise("Game", "user", [_Review("x")])
        assert calls["n"] == 2

    def test_truncated_answer_is_reported(self, monkeypatch):
        monkeypatch.setattr("app.llm.summarizer.time.sleep", lambda s: None)

        def handler(request):
            return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": '{"overview": "Обзор'}}]})

        with pytest.raises(SummaryError, match="truncated"):
            self._client(handler).summarise("Game", "user", [_Review("x")])

    def test_missing_key(self):
        with pytest.raises(SummaryError, match="OPENROUTER_API_KEY"):
            self._client(lambda r: httpx.Response(200), key="").summarise("Game", "user", [])


def test_local_rate_limiter_is_safe_across_threads():
    import threading
    import time

    limiter = LocalRateLimiter(0.05)
    stamps, lock = [], threading.Lock()

    def worker():
        for _ in range(5):
            limiter.wait()
            with lock:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 20
    assert min(gaps) >= 0.045  # 4 threads together still get one slot per interval
