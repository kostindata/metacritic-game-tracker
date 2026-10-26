"""Pure cursor / daily-reset logic. No I/O, so it is fully unit-testable.

The cursor is a position in the "virtual listing" a crawl day walks through:

    phase=new_releases, position=i   -> i-th game of the New Releases carousel
    phase=browse, page=p, position=i -> i-th card on /browse/...?page=p

Every examined listing item (processed, failed or skipped as already-done-today)
advances the cursor by one, so the next run resumes exactly after the last item
the previous run looked at.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone, tzinfo

from app.scraper.parsers import ListingItem

NEW_RELEASES = "new_releases"
BROWSE = "browse"


@dataclass(frozen=True)
class Cursor:
    day: date
    phase: str = NEW_RELEASES
    page: int = 1
    position: int = 0

    @classmethod
    def fresh(cls, day: date) -> "Cursor":
        return cls(day=day)

    def describe(self) -> str:
        if self.phase == NEW_RELEASES:
            return f"New Releases #{self.position + 1}"
        return f"Browse page {self.page}, #{self.position + 1}"


def crawl_day(now: datetime, tz: tzinfo = timezone.utc, reset_hour: int = 0) -> date:
    """The crawl "day" a moment belongs to: it rolls over at `reset_hour`:00 local time in `tz`.

    Naive datetimes are taken as UTC.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now.astimezone(tz) - timedelta(hours=reset_hour)).date()


def ensure_current_day(cursor: Cursor | None, today: date) -> tuple[Cursor, bool]:
    """Return the cursor to use for `today` and whether a daily reset happened."""
    if cursor is None or cursor.day != today:
        return Cursor.fresh(today), True
    return cursor, False


@dataclass(frozen=True)
class Step:
    item: ListingItem
    already_processed: bool
    cursor_before: Cursor
    cursor_after: Cursor


@dataclass
class WalkResult:
    exhausted: bool = False


def walk_listing(
    cursor: Cursor,
    *,
    fetch_new_releases: Callable[[], list[ListingItem]],
    fetch_browse_page: Callable[[int], list[ListingItem]],
    is_processed: Callable[[str], bool],
    max_pages: int = 50,
    result: WalkResult | None = None,
) -> Iterator[Step]:
    """Lazily yield listing items starting at `cursor`.

    Listing pages are only fetched when the walk reaches them, so a consumer that stops
    after N new games never triggers extra requests. Sets `result.exhausted` when the
    browse listing runs out (an empty page) or the per-run page budget is used up.
    """
    result = result if result is not None else WalkResult()
    seen_in_walk: set[str] = set()

    if cursor.phase == NEW_RELEASES:
        items = fetch_new_releases()
        browse_start = Cursor(day=cursor.day, phase=BROWSE, page=1, position=0)
        for pos in range(cursor.position, len(items)):
            before = replace(cursor, position=pos)
            # After the last carousel item the cursor moves straight to browse page 1.
            after = replace(cursor, position=pos + 1) if pos + 1 < len(items) else browse_start
            item = items[pos]
            done = item.slug in seen_in_walk or is_processed(item.slug)
            seen_in_walk.add(item.slug)
            yield Step(item, done, before, after)
        cursor = browse_start

    pages_fetched = 0
    while True:
        if pages_fetched >= max_pages:
            result.exhausted = True
            return
        items = fetch_browse_page(cursor.page)
        pages_fetched += 1
        if not items:
            result.exhausted = True
            return
        for pos in range(cursor.position, len(items)):
            before = replace(cursor, position=pos)
            if pos + 1 < len(items):
                after = replace(cursor, position=pos + 1)
            else:
                after = replace(cursor, page=cursor.page + 1, position=0)
            item = items[pos]
            done = item.slug in seen_in_walk or is_processed(item.slug)
            seen_in_walk.add(item.slug)
            yield Step(item, done, before, after)
        cursor = replace(cursor, page=cursor.page + 1, position=0)



class OrderedProgress:
    """Cursor bookkeeping for listing items that finish out of order.

    Items are registered in listing order and may complete in any order (concurrent
    processing). `cursor` only moves past an item once it and every item before it are
    done, so a persisted cursor never skips an unfinished game: after a crash or stop the
    next run resumes at the first unfinished item (finished ones after it are then skipped
    as already processed today).
    """

    def __init__(self, start: Cursor) -> None:
        self.cursor = start
        self._after: dict[int, Cursor] = {}
        self._done: set[int] = set()
        self._next_seq = 0      # next sequence number to hand out
        self._committed = 0     # all items below this are done and reflected in `cursor`

    def add(self, cursor_after: Cursor) -> int:
        seq = self._next_seq
        self._next_seq += 1
        self._after[seq] = cursor_after
        return seq

    def complete(self, seq: int) -> bool:
        """Mark an item done; returns True if the committed cursor moved."""
        self._done.add(seq)
        moved = False
        while self._committed in self._done:
            self._done.discard(self._committed)
            self.cursor = self._after.pop(self._committed)
            self._committed += 1
            moved = True
        return moved

    @property
    def outstanding(self) -> int:
        return self._next_seq - self._committed
