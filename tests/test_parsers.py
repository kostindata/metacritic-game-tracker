from datetime import date

import pytest

from app.scraper import nuxt
from app.scraper.parsers import (
    ParseError,
    parse_browse_page,
    parse_game_page,
    parse_new_releases,
    parse_reviews,
    parse_score_stats,
    slug_from_href,
)


@pytest.mark.parametrize(
    "href, slug",
    [
        ("/game/abyss-ring/", "abyss-ring"),
        ("https://www.metacritic.com/game/plus-protectus-/", "plus-protectus-"),
        ("/game/elden-ring/critic-reviews/?platform=pc", "elden-ring"),
        ("/movie/dune/", None),
        ("/game/", None),
        (None, None),
    ],
)
def test_slug_from_href(href, slug):
    assert slug_from_href(href) == slug


class TestNewReleases:
    def test_takes_first_twenty_cards_in_order(self, fixture_text):
        items = parse_new_releases(fixture_text("game_home.html"))
        assert len(items) == 20
        assert items[0].slug == "one-piece-grand-gourmet"
        assert items[0].title == "ONE PIECE: Grand Gourmet"
        assert items[1].slug == "call-of-duty-modern-warfare-4"
        assert items[-1].slug == "disney-pixar-toy-story-3-complete-edition"
        assert len({i.slug for i in items}) == 20

    def test_ignores_other_carousels(self, fixture_text):
        slugs = {i.slug for i in parse_new_releases(fixture_text("game_home.html"))}
        # First card of the "Upcoming Games" carousel on the same page
        assert "phantom-blade-zero" not in slugs

    def test_limit(self, fixture_text):
        assert len(parse_new_releases(fixture_text("game_home.html"), limit=5)) == 5

    def test_missing_section_raises(self):
        with pytest.raises(ParseError):
            parse_new_releases("<html><body><h2>Nothing</h2></body></html>")


class TestBrowsePage:
    def test_cards(self, fixture_text):
        items = parse_browse_page(fixture_text("browse_page1.html"))
        assert len(items) == 24
        assert items[0].slug == "abyss-ring"
        assert items[0].title == "Abyss Ring"
        assert items[1].title == "By The Sea : Pilgrimage of the Colorless"

    def test_past_last_page_is_empty(self, fixture_text):
        assert parse_browse_page(fixture_text("browse_empty.html")) == []

    def test_unrelated_page_raises(self):
        with pytest.raises(ParseError):
            parse_browse_page("<html><body>blocked</body></html>")


class TestGamePage:
    def test_full_game(self, fixture_text):
        g = parse_game_page(fixture_text("game_cod_mw4.html"), "call-of-duty-modern-warfare-4")
        assert g.title == "Call of Duty: Modern Warfare 4"
        assert g.slug == "call-of-duty-modern-warfare-4"
        assert g.url == "https://www.metacritic.com/game/call-of-duty-modern-warfare-4/"
        assert g.developers == ["Infinity Ward"]
        assert g.publishers == ["Activision"]
        assert g.genres == ["Tactical FPS"]
        assert g.release_date == date(2026, 10, 23)
        assert g.description.startswith("War erupts on the Korean Peninsula")
        assert g.cover_url == "https://www.metacritic.com/a/img/catalog/provider/7/2/7-1792136722.jpg"
        assert g.video_url == "https://cdn.jwplayer.com/players/lncRGYP5.html"

        by_name = {p.name: p for p in g.platforms}
        assert set(by_name) == {"Xbox Series X", "PlayStation 5", "Nintendo Switch 2", "PC"}
        pc = by_name["PC"]
        assert pc.is_lead and pc.slug == "pc"
        assert (pc.metascore, pc.critic_review_count) == (86, 25)
        assert (pc.user_score, pc.user_review_count) == (8.2, 168)  # lead platform's user score is on the page
        assert pc.user_stats_on_page and not by_name["PlayStation 5"].user_stats_on_page
        ps5 = by_name["PlayStation 5"]
        assert (ps5.metascore, ps5.critic_review_count, ps5.user_score) == (89, 12, None)
        switch = by_name["Nintendo Switch 2"]
        assert switch.metascore is None and switch.critic_review_count == 3  # too few reviews for a score
        assert by_name["Xbox Series X"].critic_review_count == 0

    def test_new_game_without_scores_or_video(self, fixture_text):
        g = parse_game_page(fixture_text("game_abyss_ring.html"), "abyss-ring")
        assert g.title == "Abyss Ring"
        assert g.video_url is None
        assert g.developers == ["Northern Studio LLC"] and g.publishers == ["Northern Studio LLC"]
        [pc] = g.platforms
        assert pc.metascore is None and pc.critic_review_count == 0
        assert pc.user_score is None and pc.user_review_count == 0  # "0 with 0 ratings" means tbd
        assert pc.user_stats_on_page  # known from the page: no extra request for "no ratings yet"

    def test_empty_user_score_component_is_not_fatal(self, fixture_text):
        g = parse_game_page(fixture_text("game_backrooms_realms.html"), "backrooms-realms")
        [pc] = g.platforms
        assert g.title and pc.is_lead
        assert pc.user_score is None and pc.user_review_count == 0
        assert not pc.user_stats_on_page  # empty component: the stats endpoint is asked instead

    def test_page_without_payload_raises(self):
        with pytest.raises(ParseError):
            parse_game_page("<html><body>hi</body></html>", "x")


class TestBackendJson:
    def test_user_stats(self, fixture_json):
        stats = parse_score_stats(fixture_json("user_stats_ps5.json"))
        assert stats.score == 7.8 and stats.review_count == 17

    @pytest.mark.parametrize("count", [0, 2])
    def test_stats_tbd(self, count):
        # Too few ratings: Metacritic sends score 0, the site shows "tbd"
        stats = parse_score_stats({"data": {"item": {"score": 0, "reviewCount": count}}})
        assert stats.score is None and stats.review_count == count

    def test_critic_reviews(self, fixture_json):
        page = parse_reviews(fixture_json("critic_reviews_pc.json"), "critic")
        assert page.total == 25
        assert len(page.reviews) == 10
        r = page.reviews[0]
        assert r.kind == "critic" and r.source == "GamesRadar+" and r.platform == "PC"
        assert r.score == 90 and r.review_date == date(2026, 10, 23)
        assert r.quote.startswith("Modern Warfare 4 is a confident cinematic shooter")

    def test_user_reviews(self, fixture_json):
        page = parse_reviews(fixture_json("user_reviews_pc.json"), "user")
        assert page.total == 30 and len(page.reviews) == 30
        assert page.reviews[0].source == "Defrax"
        assert all(r.kind == "user" and r.quote for r in page.reviews)

    def test_reviews_skip_empty_quotes(self):
        page = parse_reviews({"data": {"totalResults": 2, "items": [{"quote": "  "}, {"quote": "Great", "score": 9}]}}, "user")
        assert [r.quote for r in page.reviews] == ["Great"]

    def test_reviews_bad_shape(self):
        with pytest.raises(ParseError):
            parse_reviews({"data": {"nope": 1}}, "critic")


class TestNuxtDecoder:
    def test_decodes_references_and_wrappers(self):
        data = [["ShallowReactive", 1], {"data": 2, "flag": 5}, {"key": 3, "list": 4}, "value", [3, -1], True]
        assert nuxt.decode(data) == {"data": {"key": "value", "list": ["value", None]}, "flag": True}

    def test_shared_references_resolve_once(self):
        data = [{"a": 1, "b": 1}, {"x": 2}, 42]
        out = nuxt.decode(data)
        assert out["a"] is out["b"] and out["a"] == {"x": 42}

    def test_missing_payload(self):
        with pytest.raises(nuxt.NuxtPayloadError):
            nuxt.extract_payload("<html></html>")
