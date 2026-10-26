from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.crawler.cursor import BROWSE, NEW_RELEASES, Cursor, WalkResult, crawl_day, ensure_current_day, walk_listing
from app.scraper.parsers import ListingItem

DAY = date(2026, 10, 25)


def items(*slugs):
    return [ListingItem(s) for s in slugs]


class FakeListing:
    """Records which listing pages get fetched."""

    def __init__(self, new_releases, pages):
        self.new_releases_items = new_releases
        self.pages = pages  # {page_number: [ListingItem]}
        self.calls: list[str] = []

    def new_releases(self):
        self.calls.append("nr")
        return self.new_releases_items

    def browse(self, page):
        self.calls.append(f"p{page}")
        return self.pages.get(page, [])


def run(cursor, listing, processed=(), limit=20, max_pages=50):
    """Simulate one crawl run: process up to `limit` unprocessed games.

    Returns (processed slugs, final cursor, walk result) just like CrawlRunner would.
    """
    done = set(processed)
    result = WalkResult()
    picked = []
    for step in walk_listing(
        cursor,
        fetch_new_releases=listing.new_releases,
        fetch_browse_page=listing.browse,
        is_processed=lambda s: s in done,
        max_pages=max_pages,
        result=result,
    ):
        cursor = step.cursor_after
        if step.already_processed:
            continue
        picked.append(step.item.slug)
        done.add(step.item.slug)
        if len(picked) >= limit:
            break
    return picked, cursor, result, done


# --------------------------------------------------------------------------- day handling


@pytest.mark.parametrize(
    "now, hour, expected",
    [
        (datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc), 0, date(2026, 10, 25)),
        (datetime(2026, 10, 25, 23, 59, tzinfo=timezone.utc), 0, date(2026, 10, 25)),
        (datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc), 3, date(2026, 10, 24)),
        (datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc), 3, date(2026, 10, 25)),
        (datetime(2026, 10, 25, 1, 30), 0, date(2026, 10, 25)),  # naive = UTC
    ],
)
def test_crawl_day(now, hour, expected):
    assert crawl_day(now, timezone.utc, hour) == expected


MSK = ZoneInfo("Europe/Moscow")


@pytest.mark.parametrize(
    "now, expected",
    [
        # 21:00 UTC is midnight in Moscow: the new day has started
        (datetime(2026, 10, 25, 21, 0, tzinfo=timezone.utc), date(2026, 10, 26)),
        (datetime(2026, 10, 25, 20, 59, tzinfo=timezone.utc), date(2026, 10, 25)),
        (datetime(2026, 10, 26, 0, 30, tzinfo=MSK), date(2026, 10, 26)),
        # Moscow has no DST: still UTC+3 in summer
        (datetime(2026, 6, 30, 21, 0, tzinfo=timezone.utc), date(2026, 7, 1)),
    ],
)
def test_crawl_day_in_utc_plus_3(now, expected):
    assert crawl_day(now, MSK) == expected


def test_first_ever_run_starts_fresh():
    cursor, reset = ensure_current_day(None, DAY)
    assert reset and cursor == Cursor(day=DAY, phase=NEW_RELEASES, page=1, position=0)


def test_same_day_keeps_cursor():
    c = Cursor(day=DAY, phase=BROWSE, page=3, position=7)
    assert ensure_current_day(c, DAY) == (c, False)


def test_new_day_resets_to_new_releases():
    c = Cursor(day=date(2026, 10, 24), phase=BROWSE, page=9, position=4)
    cursor, reset = ensure_current_day(c, DAY)
    assert reset and cursor == Cursor.fresh(DAY)


# --------------------------------------------------------------------------- walking


def test_first_run_takes_new_releases_then_moves_to_browse():
    nr = items(*[f"nr{i}" for i in range(20)])
    listing = FakeListing(nr, {1: items("b0", "b1")})
    picked, cursor, _, _ = run(Cursor.fresh(DAY), listing)
    assert picked == [f"nr{i}" for i in range(20)]
    # Last carousel item moves the cursor straight to browse page 1; page not fetched yet.
    assert cursor == Cursor(day=DAY, phase=BROWSE, page=1, position=0)
    assert listing.calls == ["nr"]


def test_second_run_continues_in_browse_and_skips_already_processed():
    nr = items(*[f"g{i}" for i in range(20)])
    page1 = items("g0", "new1", "g5", "new2") + items(*[f"x{i}" for i in range(20)])
    listing = FakeListing(nr, {1: page1, 2: items("y0")})
    picked, cursor, _, done = run(Cursor.fresh(DAY), listing)
    picked2, cursor2, _, _ = run(cursor, listing, processed=done)
    assert picked2[:2] == ["new1", "new2"]
    assert "g0" not in picked2 and "g5" not in picked2
    assert len(picked2) == 20
    # 24 items on page 1: 2 skipped + 22 eligible, 20 taken -> stops at position 22
    assert cursor2 == Cursor(day=DAY, phase=BROWSE, page=1, position=22)


def test_rest_of_page_carries_over_to_next_run():
    page1 = items(*[f"a{i}" for i in range(24)])
    listing = FakeListing([], {1: page1, 2: items(*[f"b{i}" for i in range(24)])})
    start = Cursor(day=DAY, phase=BROWSE, page=1, position=0)
    picked, cursor, _, done = run(start, listing)
    assert picked == [f"a{i}" for i in range(20)]
    assert cursor == Cursor(day=DAY, phase=BROWSE, page=1, position=20)

    picked2, cursor2, _, _ = run(cursor, listing, processed=done)
    assert picked2 == ["a20", "a21", "a22", "a23"] + [f"b{i}" for i in range(16)]
    assert cursor2 == Cursor(day=DAY, phase=BROWSE, page=2, position=16)


def test_page_boundary_cursor_points_to_next_page():
    listing = FakeListing([], {1: items(*[f"a{i}" for i in range(20)])})
    picked, cursor, _, _ = run(Cursor(day=DAY, phase=BROWSE), listing)
    assert len(picked) == 20
    assert cursor == Cursor(day=DAY, phase=BROWSE, page=2, position=0)
    assert listing.calls == ["p1"]  # page 2 is not fetched until the next run


def test_resume_mid_new_releases():
    nr = items(*[f"nr{i}" for i in range(20)])
    listing = FakeListing(nr, {1: items("b0")})
    picked, cursor, _, _ = run(Cursor(day=DAY, phase=NEW_RELEASES, position=15), listing, limit=20)
    assert picked == ["nr15", "nr16", "nr17", "nr18", "nr19", "b0"]


def test_listing_runs_out():
    listing = FakeListing([], {1: items("a", "b"), 2: []})
    picked, cursor, result, _ = run(Cursor(day=DAY, phase=BROWSE), listing)
    assert picked == ["a", "b"]
    assert result.exhausted
    assert cursor == Cursor(day=DAY, phase=BROWSE, page=2, position=0)


def test_page_budget_stops_endless_skipping():
    pages = {p: items(f"s{p}") for p in range(1, 100)}
    listing = FakeListing([], pages)
    processed = {f"s{p}" for p in range(1, 100)}
    picked, cursor, result, _ = run(Cursor(day=DAY, phase=BROWSE), listing, processed=processed, max_pages=5)
    assert picked == [] and result.exhausted
    assert cursor.page == 6 and len(listing.calls) == 5


def test_duplicates_within_a_walk_are_skipped():
    listing = FakeListing(items("a", "b"), {1: items("a", "c")})
    picked, _, _, _ = run(Cursor.fresh(DAY), listing)
    assert picked == ["a", "b", "c"]


def test_failed_game_still_advances_cursor():
    """The consumer advances on every step whether processing succeeded or not."""
    listing = FakeListing([], {1: items("bad", "good")})
    steps = list(
        walk_listing(Cursor(day=DAY, phase=BROWSE), fetch_new_releases=list, fetch_browse_page=listing.browse,
                     is_processed=lambda s: False, max_pages=1)
    )
    assert [s.cursor_after.position for s in steps] == [1, 0]
    assert steps[1].cursor_after.page == 2


def test_500_game_run_spans_new_releases_and_many_browse_pages():
    """Default run size: 20 New Releases, then 480 from the browse listing (24 per page)."""
    nr = items(*[f"nr{i}" for i in range(20)])
    pages = {p: items(*[f"p{p}g{i}" for i in range(24)]) for p in range(1, 60)}
    pages[1] = items("nr0", "nr1") + pages[1][2:]  # overlap with New Releases is skipped
    listing = FakeListing(nr, pages)

    picked, cursor, result, done = run(Cursor.fresh(DAY), listing, limit=500, max_pages=100)
    assert len(picked) == 500 and len(set(picked)) == 500
    assert picked[:20] == [f"nr{i}" for i in range(20)]
    # 480 from browse: page 1 has 22 eligible, then 458 = 19 full pages (456) + 2 on page 21
    assert cursor == Cursor(day=DAY, phase=BROWSE, page=21, position=2)
    assert not result.exhausted
    assert listing.calls == ["nr"] + [f"p{p}" for p in range(1, 22)]

    # The next run continues exactly there
    picked2, cursor2, _, _ = run(cursor, listing, processed=done, limit=500, max_pages=100)
    assert picked2[0] == "p21g2" and len(picked2) == 500


# --------------------------------------------------------------------------- concurrent completion


def _c(pos):
    return Cursor(day=DAY, phase=BROWSE, page=1, position=pos)


def test_ordered_progress_waits_for_earlier_games():
    from app.crawler.cursor import OrderedProgress

    p = OrderedProgress(_c(0))
    a, b, c = p.add(_c(1)), p.add(_c(2)), p.add(_c(3))
    assert not p.complete(c)          # finished early, but a and b are still running
    assert p.cursor == _c(0)
    assert not p.complete(b)
    assert p.cursor == _c(0)
    assert p.complete(a)               # a done -> a, b, c all committed at once
    assert p.cursor == _c(3) and p.outstanding == 0


def test_ordered_progress_never_passes_an_unfinished_game():
    import random
    from app.crawler.cursor import OrderedProgress

    rng = random.Random(7)
    for _ in range(200):
        p = OrderedProgress(_c(0))
        seqs = [p.add(_c(i + 1)) for i in range(30)]
        done = set()
        for seq in rng.sample(seqs, k=rng.randint(0, 30)):
            p.complete(seq)
            done.add(seq)
            # every item before the committed position is finished
            assert all(i in done for i in range(p.cursor.position))
        first_unfinished = next((i for i in range(30) if i not in done), 30)
        assert p.cursor == _c(first_unfinished)
