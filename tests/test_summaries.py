import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.crawler.upsert import summary_action
from app.llm.fingerprint import reviews_fingerprint
from app.llm.summarizer import SummaryError, parse_summary


def action(status=None, old_fp=None, new_fp="A", fetched=10, minimum=3):
    return summary_action(
        existing_status=status, existing_fingerprint=old_fp, new_fingerprint=new_fp,
        fetched_reviews=fetched, min_reviews=minimum,
    )


class TestSummaryAction:
    def test_first_time_generates(self):
        assert action() == "regenerate"

    def test_same_reviews_reuse_cached_summary(self):
        assert action(status="ok", old_fp="A", new_fp="A") == "keep"

    def test_new_written_reviews_regenerate(self):
        assert action(status="ok", old_fp="A", new_fp="B") == "regenerate"

    def test_too_few_reviews(self):
        assert action(fetched=2) == "not_enough"
        assert action(fetched=0) == "not_enough"
        assert action(status="not_enough", fetched=2) == "keep"

    def test_crossing_threshold_generates(self):
        assert action(status="not_enough", fetched=3) == "regenerate"

    def test_never_finished_generation_stays_marked(self):
        assert action(status="error", old_fp=None) == "regenerate"
        assert action(status="outdated", old_fp="A", new_fp="A") == "regenerate"


class TestFingerprint:
    def r(self, quote, source="IGN", platform="PC", day=date(2026, 10, 1)):
        return SimpleNamespace(platform=platform, source=source, review_date=day, quote=quote)

    def test_order_and_whitespace_do_not_matter(self):
        a = [self.r("Great game"), self.r("Too short", source="GameSpot")]
        b = [self.r("Too   short", source="GameSpot"), self.r("Great game")]
        assert reviews_fingerprint(a) == reviews_fingerprint(b)

    def test_added_review_changes_fingerprint(self):
        a = [self.r("Great game")]
        assert reviews_fingerprint(a) != reviews_fingerprint(a + [self.r("New opinion", source="Polygon")])

    def test_edited_text_changes_fingerprint(self):
        assert reviews_fingerprint([self.r("Great game")]) != reviews_fingerprint([self.r("Great game!")])


OVERVIEW = "Критики в целом тепло приняли игру: " + "подробный обзор консенсуса. " * 5


def payload(likes, dislikes, overview=OVERVIEW):
    return json.dumps({"overview": overview, "likes": likes, "dislikes": dislikes}, ensure_ascii=False)


class TestParseSummary:
    def test_valid(self):
        s = parse_summary(payload(list("abcdefg"), ["- Длинный пункт с контекстом.", "y", "z"]))
        assert s.likes == list("abcdef")  # trimmed to 6
        assert s.dislikes == ["Длинный пункт с контекстом.", "y", "z"]  # bullet marker stripped
        assert s.overview.startswith("Критики в целом")

    def test_code_fence(self):
        s = parse_summary("```json\n" + payload(["a", "b", "c"], ["d", "e", "f"]) + "\n```")
        assert s.likes == ["a", "b", "c"]

    @pytest.mark.parametrize(
        "content",
        [
            "not json",
            payload("a", []),
            payload(["a", "b"], ["c", "d", "e"]),
            payload(["a", "b", "c"], ["d", "e", "f"], overview="Коротко."),
            payload(["a", "b", "c"], ["d", "e", "f"], overview=None),
        ],
    )
    def test_invalid(self, content):
        with pytest.raises(SummaryError):
            parse_summary(content)
