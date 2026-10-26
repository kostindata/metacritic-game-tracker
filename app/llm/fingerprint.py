"""Identity of the set of written reviews a summary is based on.

Two review sets get the same fingerprint exactly when they contain the same reviews (same
platform, source, date and text), whatever their order. A summary is regenerated only when the
fingerprint of the currently fetched reviews differs from the one it was generated from, i.e.
when written reviews were added (or removed). New star ratings without text do not count.
"""

import hashlib
import json
from collections.abc import Iterable
from typing import Protocol


class ReviewLike(Protocol):
    platform: str | None
    source: str | None
    review_date: object
    quote: str


def reviews_fingerprint(reviews: Iterable[ReviewLike]) -> str:
    items = sorted(
        (
            r.platform or "",
            r.source or "",
            r.review_date.isoformat() if r.review_date else "",
            " ".join((r.quote or "").split()),
        )
        for r in reviews
    )
    return hashlib.sha256(json.dumps(items, ensure_ascii=False).encode()).hexdigest()
