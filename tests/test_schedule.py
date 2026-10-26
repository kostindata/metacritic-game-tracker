from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.schedule import next_crawl_time, next_reset_time


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_next_crawl_time():
    assert next_crawl_time(utc(2026, 10, 25, 14, 20), 0) == utc(2026, 10, 25, 15, 0)
    assert next_crawl_time(utc(2026, 10, 25, 14, 0), 0) == utc(2026, 10, 25, 15, 0)
    assert next_crawl_time(utc(2026, 10, 25, 14, 10), 30) == utc(2026, 10, 25, 14, 30)
    assert next_crawl_time(utc(2026, 10, 25, 23, 59), 0) == utc(2026, 10, 26, 0, 0)


def test_next_reset_time():
    assert next_reset_time(utc(2026, 10, 25, 14, 20), 0) == utc(2026, 10, 26, 0, 0)
    assert next_reset_time(utc(2026, 10, 25, 1, 0), 3) == utc(2026, 10, 25, 3, 0)


MSK = ZoneInfo("Europe/Moscow")


def test_next_run_and_reset_in_utc_plus_3():
    now = utc(2026, 10, 25, 14, 20)  # 17:20 MSK
    nxt = next_crawl_time(now, 0, MSK)
    assert nxt == utc(2026, 10, 25, 15, 0) and nxt.utcoffset().total_seconds() == 3 * 3600
    # local midnight = 21:00 UTC
    assert next_reset_time(now, 0, MSK) == utc(2026, 10, 25, 21, 0)
    assert next_reset_time(utc(2026, 10, 25, 21, 30), 0, MSK) == utc(2026, 10, 26, 21, 0)
