"""Integration tests against a real PostgreSQL database (schema from `alembic upgrade head`).

Skipped unless TEST_DATABASE_URL points at a disposable database; every table is truncated
between tests. With Docker:

    docker compose exec postgres createdb -U mc mc_test
    docker compose run --rm --no-deps -e DATABASE_URL=postgresql+psycopg://mc:mc@postgres:5432/mc_test \
        web alembic upgrade head
    docker compose run --rm --no-deps -e TEST_DATABASE_URL=postgresql+psycopg://mc:mc@postgres:5432/mc_test \
        -e OPENROUTER_API_KEY= web pytest
"""

import os
from datetime import date, datetime, timezone

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")

if TEST_DB:
    # Must happen before anything reads the cached settings / engine.
    os.environ["DATABASE_URL"] = TEST_DB
    os.environ["OPENROUTER_API_KEY"] = ""

from sqlalchemy import select, text  # noqa: E402

from app.crawler.upsert import plan_summary, replace_reviews, upsert_game  # noqa: E402
from app.llm.summarizer import Summary, SummaryError, claim_for_generation, generate_summary  # noqa: E402
from app.models import Base, Game, GameSimilar, ReviewSummary  # noqa: E402
from app.scraper.parsers import GameDetail, PlatformData, ReviewData  # noqa: E402

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def sessions():
    from app.config import get_settings
    from app.db import get_engine, get_sessionmaker

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    assert get_settings().database_url == TEST_DB
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return get_sessionmaker()


def detail(slug="abyss-ring", user_score=8.0, user_count=40, stats_failed=False, platforms=("PC", "PlayStation 5")):
    plats = [
        PlatformData(name=name, slug=name.lower().replace(" ", "-"), metascore=80 + i, critic_review_count=10,
                     release_date=date(2026, 10, 1), is_lead=i == 0, user_score=user_score,
                     user_review_count=user_count, user_stats_failed=stats_failed and i == 1)
        for i, name in enumerate(platforms)
    ]
    return GameDetail(slug=slug, title="Abyss Ring", url=f"https://www.metacritic.com/game/{slug}/",
                      description="d", cover_url=None, video_url=None, genres=["Action"], developers=["Dev"],
                      release_date=date(2026, 10, 1), platforms=plats)


def reviews(n, text_="Great combat"):
    return [ReviewData(kind="critic", quote=f"{text_} #{i}", score=80, source=f"Outlet {i}",
                       platform="PC", review_date=date(2026, 10, 2)) for i in range(n)]


class FakeLLM:
    def __init__(self, on_call=None, fail=False):
        self.calls, self.on_call, self.fail = 0, on_call, fail

    def summarise(self, title, kind, revs):
        self.calls += 1
        if self.on_call:
            self.on_call()
        if self.fail:
            raise SummaryError("boom")
        return Summary(overview="o" * 150, likes=["a", "b", "c"], dislikes=["x", "y", "z"])


# --------------------------------------------------------------------------- crawl writes


def test_upsert_inserts_then_updates_without_duplicates(sessions):
    with sessions.begin() as s:
        game, inserted = upsert_game(s, detail(), NOW)
        assert inserted and len(game.platforms) == 2
    with sessions.begin() as s:
        game, inserted = upsert_game(s, detail(platforms=("PC",)), NOW)
        assert not inserted
        assert [p.platform for p in game.platforms] == ["PC"]  # removed platform is dropped
    with sessions() as s:
        assert s.scalar(select(text("count(*)")).select_from(Game)) == 1


def test_failed_user_stats_keep_the_stored_score(sessions):
    with sessions.begin() as s:
        upsert_game(s, detail(user_score=8.0, user_count=40), NOW)
    # PlayStation 5's stats request fails this time: its parsed score is None.
    d = detail(user_score=None, user_count=0, stats_failed=True)
    d.platforms[0].user_score, d.platforms[0].user_review_count = 6.0, 50
    with sessions.begin() as s:
        game, _ = upsert_game(s, d, NOW)
        ps5 = next(p for p in game.platforms if p.platform == "PlayStation 5")
        assert (ps5.user_score, ps5.user_review_count) == (8.0, 40)
        assert game.best_user_score == 8.0 and game.user_review_total == 90


# --------------------------------------------------------------------------- summaries


def _prepare_summary(sessions, n=5):
    with sessions.begin() as s:
        game, _ = upsert_game(s, detail(), NOW)
        replace_reviews(s, game.id, "critic", reviews(n))
        assert plan_summary(s, game, "critic", reviews(n), 3) == "regenerate"
        return game.id


def _summary(sessions, game_id):
    with sessions() as s:
        return s.scalar(select(ReviewSummary).where(ReviewSummary.game_id == game_id, ReviewSummary.kind == "critic"))


def test_summary_generated_once_and_reused(sessions):
    game_id = _prepare_summary(sessions)
    with sessions.begin() as s:
        assert claim_for_generation(s, game_id) == ["critic"]
    with sessions.begin() as s:
        assert claim_for_generation(s, game_id) == []  # concurrent views claim only once
    llm = FakeLLM()
    assert generate_summary(sessions, llm, game_id, "critic") == "ok"
    assert _summary(sessions, game_id).status == "ok" and llm.calls == 1
    # Same reviews on the next crawl: kept, never claimed again.
    with sessions.begin() as s:
        game = s.get(Game, game_id)
        assert plan_summary(s, game, "critic", reviews(5), 3) == "keep"
        assert claim_for_generation(s, game_id) == []


def test_new_reviews_mark_summary_outdated(sessions):
    game_id = _prepare_summary(sessions)
    with sessions.begin() as s:
        claim_for_generation(s, game_id)
    generate_summary(sessions, FakeLLM(), game_id, "critic")
    with sessions.begin() as s:
        game = s.get(Game, game_id)
        replace_reviews(s, game_id, "critic", reviews(6))
        assert plan_summary(s, game, "critic", reviews(6), 3) == "regenerate"
    summary = _summary(sessions, game_id)
    assert summary.status == "outdated" and summary.likes  # old text stays visible meanwhile


def test_reviews_replaced_during_generation_leave_summary_outdated(sessions):
    game_id = _prepare_summary(sessions)
    with sessions.begin() as s:
        claim_for_generation(s, game_id)

    def crawl_meanwhile():
        with sessions.begin() as s:
            game = s.get(Game, game_id)
            replace_reviews(s, game_id, "critic", reviews(7, "Newer take"))
            plan_summary(s, game, "critic", reviews(7, "Newer take"), 3)  # leaves "pending" alone

    assert generate_summary(sessions, FakeLLM(on_call=crawl_meanwhile), game_id, "critic") == "outdated"
    summary = _summary(sessions, game_id)
    assert summary.status == "outdated" and summary.review_count == 5
    with sessions.begin() as s:
        assert claim_for_generation(s, game_id) == ["critic"]  # next view regenerates


def test_result_discarded_when_crawl_found_too_few_reviews(sessions):
    game_id = _prepare_summary(sessions)
    with sessions.begin() as s:
        claim_for_generation(s, game_id)

    def crawl_meanwhile():
        with sessions.begin() as s:
            game = s.get(Game, game_id)
            replace_reviews(s, game_id, "critic", reviews(1))
            plan_summary(s, game, "critic", reviews(1), 3)

    assert generate_summary(sessions, FakeLLM(on_call=crawl_meanwhile), game_id, "critic") == "not_enough"
    summary = _summary(sessions, game_id)
    assert summary.status == "not_enough" and not summary.likes


def test_llm_failure_is_recorded_and_retried_later(sessions):
    game_id = _prepare_summary(sessions)
    with sessions.begin() as s:
        claim_for_generation(s, game_id)
    assert generate_summary(sessions, FakeLLM(fail=True), game_id, "critic") == "error"
    with sessions.begin() as s:
        assert claim_for_generation(s, game_id) == []  # not immediately
        s.execute(text("UPDATE review_summaries SET updated_at = now() - interval '2 minutes'"))
    with sessions.begin() as s:
        assert claim_for_generation(s, game_id) == ["critic"]


# --------------------------------------------------------------------------- similar games


def test_refresh_similar_replaces_table(sessions):
    from app.similarity import refresh_similar

    with sessions.begin() as s:
        upsert_game(s, detail("a"), NOW)
        upsert_game(s, detail("b"), NOW)
    for _ in range(2):  # idempotent
        with sessions.begin() as s:
            assert refresh_similar(s) == 2
    with sessions() as s:
        assert len(s.scalars(select(GameSimilar)).all()) == 2


# --------------------------------------------------------------------------- web


@pytest.fixture
def client(sessions):
    from fastapi.testclient import TestClient

    from app.web.main import app

    with sessions.begin() as s:
        upsert_game(s, detail("abyss-ring"), NOW)
    return TestClient(app)


HTML = {"Accept": "text/html"}


def test_game_list_search_and_lenient_params(client):
    r = client.get("/", params={"q": "abyss", "platform": "PC"}, headers=HTML)
    assert r.status_code == 200 and "/games/abyss-ring" in r.text
    assert "Ничего не найдено" in client.get("/", params={"q": "100%_"}, headers=HTML).text
    for params in ({"page": "abc"}, {"sort": "bogus"}, {"order": "sideways"}, {"page": "99999"}):
        r = client.get("/", params=params, headers=HTML)
        assert r.status_code == 200 and "/games/abyss-ring" in r.text, params


def test_htmx_filter_returns_grid_only(client):
    r = client.get("/", params={"q": "abyss"}, headers={"HX-Request": "true", "HX-Target": "results"})
    assert r.status_code == 200 and "<html" not in r.text and "/games/abyss-ring" in r.text


def test_game_page_and_summary_block(client):
    r = client.get("/games/abyss-ring", headers=HTML)
    assert r.status_code == 200 and "Abyss Ring" in r.text and "Похожие игры" in r.text
    r = client.get("/games/abyss-ring/summaries", headers={"HX-Request": "true"})
    assert r.status_code == 200 and 'id="summaries"' in r.text


def test_missing_game_is_an_html_page_for_browsers_and_json_for_apis(client):
    r = client.get("/games/nope", headers=HTML)
    assert r.status_code == 404 and "Игра не найдена" in r.text and "<html" in r.text
    r = client.get("/games/nope")
    assert r.status_code == 404 and r.json() == {"detail": "Game not found"}


def test_cross_site_post_cannot_start_a_crawl(client):
    r = client.post("/runs", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


# --------------------------------------------------------------------------- cover images


def test_crawl_stores_the_cover_once(sessions, tmp_path, monkeypatch):
    from app.config import get_settings
    from app.crawler.runner import CrawlRunner

    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 64

    class FakeMetacritic:
        downloads = 0

        def game(self, slug):
            d = detail(slug, platforms=("PC",))
            d.cover_url = "https://www.metacritic.com/a/img/catalog/x.jpg"
            d.platforms[0].critic_review_count = d.platforms[0].user_review_count = 0
            d.platforms[0].user_stats_on_page = True
            return d

        def get_bytes(self, url, max_bytes):
            FakeMetacritic.downloads += 1
            return jpeg

    monkeypatch.setenv("COVERS_DIR", str(tmp_path))
    get_settings.cache_clear()
    runner = CrawlRunner(get_settings(), FakeMetacritic(), sessions, lock=None, lock_token="t")
    for _ in range(2):
        assert runner._process_one(0, date(2026, 10, 27), "abyss-ring") in ("inserted", "updated")
    with sessions() as s:
        game = s.scalar(select(Game).where(Game.slug == "abyss-ring"))
        assert game.cover_file and (tmp_path / game.cover_file).read_bytes() == jpeg
    assert FakeMetacritic.downloads == 1


def test_failed_cover_download_keeps_the_game(sessions, tmp_path, monkeypatch):
    from app.config import get_settings
    from app.crawler.runner import CrawlRunner
    from app.models import CrawlError

    class FakeMetacritic:
        def game(self, slug):
            d = detail(slug, platforms=("PC",))
            d.cover_url = "https://www.metacritic.com/a/img/catalog/y.jpg"
            d.platforms[0].critic_review_count = d.platforms[0].user_review_count = 0
            d.platforms[0].user_stats_on_page = True
            return d

        def get_bytes(self, url, max_bytes):
            return b"<html>blocked</html>"

    monkeypatch.setenv("COVERS_DIR", str(tmp_path))
    get_settings.cache_clear()
    runner = CrawlRunner(get_settings(), FakeMetacritic(), sessions, lock=None, lock_token="t")
    assert runner._process_one(0, date(2026, 10, 27), "abyss-ring") == "inserted"
    with sessions() as s:
        game = s.scalar(select(Game).where(Game.slug == "abyss-ring"))
        assert game.cover_file is None and game.cover_url.endswith("y.jpg")
        assert s.scalar(select(CrawlError.stage)) == "cover"
