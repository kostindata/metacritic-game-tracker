from datetime import date

import pytest

from app.similarity import Features, date_closeness, jaccard, rank_similar, similarity


def f(genres=(), devs=(), platforms=(), released=None):
    return Features.of(genres, devs, platforms, released)


def test_jaccard():
    assert jaccard(frozenset("ab"), frozenset("bc")) == pytest.approx(1 / 3)
    assert jaccard(frozenset(), frozenset("a")) == 0.0


@pytest.mark.parametrize(
    "a, b, expected",
    [
        (date(2026, 1, 1), date(2026, 1, 1), 1.0),
        (date(2026, 1, 1), date(2027, 1, 1), 1 - 365 / 730),
        (date(2020, 1, 1), date(2026, 1, 1), 0.0),
        (None, date(2026, 1, 1), 0.0),
    ],
)
def test_date_closeness(a, b, expected):
    assert date_closeness(a, b) == pytest.approx(expected)


def test_identical_games_score_one():
    a = f(["RPG"], ["FromSoftware"], ["PC", "PS5"], date(2026, 1, 1))
    assert similarity(a, a) == pytest.approx(1.0)


def test_weights():
    base = f(["RPG"], ["Studio"], ["PC"], date(2026, 1, 1))
    only_genre = f(["rpg"], ["Other"], ["Switch"], None)  # case-insensitive
    only_dev = f(["Racing"], ["studio"], ["Switch"], None)
    only_platform = f(["Racing"], ["Other"], ["PC"], None)
    assert similarity(base, only_genre) == pytest.approx(0.5)
    assert similarity(base, only_dev) == pytest.approx(0.2)
    assert similarity(base, only_platform) == pytest.approx(0.2)


def test_genres_weigh_most():
    target = f(["RPG", "Action"], ["A"], ["PC"], date(2026, 1, 1))
    genre_match = f(["RPG", "Action"], ["B"], ["Switch"], date(2025, 1, 1))
    dev_and_platform = f(["Puzzle"], ["A"], ["PC"], date(2026, 1, 1))
    # 0.5 + 0.1 * 0.5  vs  0.2 + 0.2 + 0.1
    assert similarity(target, genre_match) == pytest.approx(0.55)
    assert similarity(target, dev_and_platform) == pytest.approx(0.5)
    ranked = rank_similar(target, [("dev", dev_and_platform), ("genre", genre_match)])
    assert ranked[0][0] == "genre"


def test_rank_requires_something_shared_and_limits():
    target = f(["RPG"], ["A"], ["PC"], date(2026, 1, 1))
    candidates = [(f"g{i}", f(["RPG"], [], [], date(2026, 1, 1 + i))) for i in range(10)]
    candidates.append(("same-date-only", f(["Racing"], ["Z"], ["Switch"], date(2026, 1, 1))))
    ranked = rank_similar(target, candidates, limit=6)
    assert len(ranked) == 6
    assert "same-date-only" not in [k for k, _ in ranked]
    assert [k for k, _ in ranked][:3] == ["g0", "g1", "g2"]  # closer release dates first
    scores = [s for _, s in ranked]
    assert scores == sorted(scores, reverse=True)


def test_compute_all_similar_uses_shared_traits_and_falls_back_to_platform():
    from app.similarity import compute_all_similar

    games = {
        1: f(["RPG"], ["Studio A"], ["PC"], date(2026, 1, 1)),
        2: f(["RPG"], ["Studio B"], ["PC"], date(2026, 2, 1)),
        3: f(["RPG"], ["Studio A"], ["Switch"], date(2020, 1, 1)),
        4: f(["Racing"], ["Studio C"], ["PC"], date(2026, 1, 5)),   # only a platform in common
        5: f(["Puzzle"], ["Studio D"], ["Xbox"], date(2026, 1, 1)),  # nothing in common with anyone
    }
    result = compute_all_similar(games, limit=3)
    assert [gid for gid, _ in result[1]][:2] == [2, 3]   # shared genre beats platform-only
    assert 5 not in [gid for gid, _ in result[1]]
    assert result[5] == []                              # no shared traits -> no suggestions
    # Racing has no genre/developer matches, so same-platform games are used as a fallback
    assert {gid for gid, _ in result[4]} == {1, 2}
    for items in result.values():
        scores = [s for _, s in items]
        assert scores == sorted(scores, reverse=True)
    assert all(gid not in [sid for sid, _ in items] for gid, items in result.items())  # never itself


def test_compute_all_similar_scales_without_all_pairs():
    from app.similarity import compute_all_similar

    # 3000 PC games across 30 genres: candidates come from the genre index, not all pairs.
    games = {i: f([f"G{i % 30}"], [f"Dev{i}"], ["PC"], date(2026, 1, 1 + i % 28)) for i in range(3000)}
    import time
    t = time.perf_counter()
    result = compute_all_similar(games, limit=6)
    assert time.perf_counter() - t < 20
    assert all(len(v) == 6 for v in result.values())
    assert all(games[sid].genres == games[0].genres for sid, _ in result[0])
