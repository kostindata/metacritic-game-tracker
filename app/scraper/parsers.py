"""Pure parsing functions: Metacritic HTML/JSON in, plain dataclasses out."""

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from app.scraper import nuxt

BASE_URL = "https://www.metacritic.com"
IMAGE_BASE = "https://www.metacritic.com/a/img/catalog"
_GAME_PATH_RE = re.compile(r"^/game/([^/?#]+)/?")


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class ListingItem:
    slug: str
    title: str | None = None


@dataclass
class PlatformData:
    name: str
    slug: str | None
    metascore: int | None
    critic_review_count: int
    release_date: date | None
    is_lead: bool = False
    user_score: float | None = None
    user_review_count: int = 0
    # True when the game page already carried this platform's user score (lead platform),
    # even if it is empty: no separate stats request is needed then.
    user_stats_on_page: bool = False
    # Set by the crawler when the separate stats request failed: the stored score is kept.
    user_stats_failed: bool = False


@dataclass
class GameDetail:
    slug: str
    title: str
    url: str
    description: str | None
    cover_url: str | None
    video_url: str | None
    developers: list[str] = field(default_factory=list)
    publishers: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    release_date: date | None = None
    platforms: list[PlatformData] = field(default_factory=list)


@dataclass(frozen=True)
class ScoreStats:
    score: float | None
    review_count: int


@dataclass(frozen=True)
class ReviewData:
    kind: str
    quote: str
    score: float | None
    source: str | None
    platform: str | None
    review_date: date | None


@dataclass(frozen=True)
class ReviewPage:
    reviews: list[ReviewData]
    total: int


# --------------------------------------------------------------------------- helpers


def slug_from_href(href: str | None) -> str | None:
    if not href:
        return None
    match = _GAME_PATH_RE.match(urlparse(href).path)
    return match.group(1) if match else None


def game_url(slug: str) -> str:
    return f"{BASE_URL}/game/{slug}/"


def _parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None  # e.g. "tbd"


def _http_url(value: Any) -> str | None:
    """An absolute http(s) URL, or None. Covers and trailers are rendered as links and iframes,
    so anything else (relative paths, `javascript:`, lists from odd JSON-LD) is dropped."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("//"):
        value = "https:" + value
    return value if urlparse(value).scheme in ("http", "https") and len(value) <= 1000 else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _unique(values: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for v in values:
        v = v.strip()
        if v:
            seen.setdefault(v, None)
    return list(seen)


def _listing_items(nodes) -> list[ListingItem]:
    items: list[ListingItem] = []
    seen: set[str] = set()
    for node in nodes:
        slug = slug_from_href(node.attributes.get("href"))
        if not slug or slug in seen:
            continue
        seen.add(slug)
        title_node = node.css_first('[data-testid="product-title"], [data-testid="product-card-title"]')
        img = node.css_first("img[alt]")
        title = title_node.text(strip=True) if title_node else (img.attributes.get("alt") if img else None)
        items.append(ListingItem(slug=slug, title=title or None))
    return items


# --------------------------------------------------------------------------- listings


def parse_new_releases(html: str, limit: int = 20) -> list[ListingItem]:
    """Games in the "New Releases" carousel of https://www.metacritic.com/game/."""
    tree = HTMLParser(html)
    section = tree.css_first('section[aria-label="New Releases content"]')
    if section is None:
        # Fallback: locate the carousel whose heading reads "New Releases".
        for heading in tree.css("h2, h3"):
            if heading.text(strip=True).lower() == "new releases":
                node = heading.parent
                while node is not None and node.css_first('[data-testid="product-card"]') is None:
                    node = node.parent
                section = node
                break
    if section is None:
        raise ParseError("New Releases section not found")
    items = _listing_items(section.css('a[data-testid="product-card-content"], a[href^="/game/"]'))
    if not items:
        raise ParseError("New Releases section has no games")
    return items[:limit]


def parse_browse_page(html: str) -> list[ListingItem]:
    """Game cards on a /browse/game/... page, in display order. Empty list = past the end."""
    tree = HTMLParser(html)
    if tree.css_first('[data-testid="navigation-pagination"], [data-testid="filter-results"], '
                      '[data-testid="dropdown-sort"]') is None:
        raise ParseError("not a browse page")
    cards = tree.css('[data-testid="filter-results"] a[href^="/game/"]')
    return _listing_items(cards)


# --------------------------------------------------------------------------- game page


def _components(page: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for comp in page.get("components") or []:
        if not isinstance(comp, dict):
            continue
        name = (comp.get("meta") or {}).get("componentName")
        if name:
            out[name] = comp
    return out


def _image_url(images: list[dict[str, Any]] | None) -> str | None:
    by_type: dict[str, dict[str, Any]] = {}
    for img in images or []:
        if isinstance(img, dict) and img.get("typeName"):
            by_type.setdefault(img["typeName"], img)
    for preferred in ("cardImage", "mainImage"):
        img = by_type.get(preferred)
        if img and img.get("bucketPath"):
            return f"{IMAGE_BASE}{img['bucketPath']}"
    return None


def _video_url(video: dict[str, Any] | None) -> str | None:
    if not isinstance(video, dict):
        return None
    if _http_url(video.get("embedUrl")):
        return _http_url(video["embedUrl"])
    if isinstance(video.get("jwPlayerId"), str) and re.fullmatch(r"[A-Za-z0-9_-]+", video["jwPlayerId"]):
        return f"https://cdn.jwplayer.com/players/{video['jwPlayerId']}.html"
    return _http_url(video.get("url"))


def _ld_json(html: str) -> dict[str, Any] | None:
    tree = HTMLParser(html)
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and data.get("@type") == "VideoGame":
            return data
    return None


def _user_stats_component(comps: dict[str, dict[str, Any]]) -> ScoreStats | None:
    comp = comps.get("user-score-summary")
    if not comp or not comp.get("data"):
        return None  # e.g. brand-new games: the component is present but empty
    try:
        return parse_score_stats(comp)
    except ParseError:
        return None


def parse_game_page(html: str, slug: str) -> GameDetail:
    try:
        data = nuxt.page_data(html)
    except nuxt.NuxtPayloadError as exc:
        raise ParseError(str(exc)) from exc

    page = next(
        (v for k, v in data.items() if k.startswith("loadPage:games:") and isinstance(v, dict)),
        None,
    )
    if page is None:
        raise ParseError("game page payload not found")
    comps = _components(page)
    product = ((comps.get("product") or {}).get("data") or {}).get("item")
    if not isinstance(product, dict) or not product.get("title"):
        raise ParseError("product component missing")

    companies = ((product.get("production") or {}).get("companies")) or []
    developers = _unique([c["name"] for c in companies if c.get("typeName") == "Developer" and c.get("name")])
    publishers = _unique([c["name"] for c in companies if c.get("typeName") == "Publisher" and c.get("name")])
    genres = _unique([g["name"] for g in product.get("genres") or [] if isinstance(g, dict) and g.get("name")])

    lead_user = _user_stats_component(comps)
    platforms: list[PlatformData] = []
    for p in product.get("platforms") or []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        summary = p.get("criticScoreSummary") or {}
        pd = PlatformData(
            name=p["name"],
            slug=p.get("slug"),
            metascore=_to_int(summary.get("score")),
            critic_review_count=_to_int(summary.get("reviewCount")) or 0,
            release_date=_parse_date(p.get("releaseDate")),
            is_lead=bool(p.get("isLeadPlatform")),
        )
        if pd.is_lead and lead_user is not None:
            pd.user_score, pd.user_review_count = lead_user.score, lead_user.review_count
            pd.user_stats_on_page = True
        platforms.append(pd)

    ld = _ld_json(html) or {}
    description = _text(product.get("description")) or _text(ld.get("description")) or None
    cover = _image_url(product.get("images")) or _http_url(ld.get("image"))
    video = _video_url(product.get("video"))
    if video is None and isinstance(ld.get("trailer"), dict):
        video = _http_url(ld["trailer"].get("embedUrl")) or _http_url(ld["trailer"].get("contentUrl"))

    release = _parse_date(product.get("releaseDate"))
    if release is None:
        dates = [p.release_date for p in platforms if p.release_date]
        release = min(dates) if dates else None

    return GameDetail(
        slug=product.get("slug") or slug,
        title=html_lib.unescape(product["title"]).strip(),
        url=game_url(product.get("slug") or slug),
        description=description,
        cover_url=cover,
        video_url=video,
        developers=developers,
        publishers=publishers,
        genres=genres,
        release_date=release,
        platforms=platforms,
    )


# --------------------------------------------------------------------------- backend JSON


def _unwrap(payload: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ParseError("expected JSON object")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise ParseError("JSON has no data object")
    return data


def parse_score_stats(payload: dict[str, Any] | str) -> ScoreStats:
    """`.../{critic|user}/games/{slug}/platform/{p}/stats/web` response (or the SSR component)."""
    item = _unwrap(payload).get("item")
    if not isinstance(item, dict):
        raise ParseError("stats JSON has no item")
    count = _to_int(item.get("reviewCount")) or 0
    score = _to_float(item.get("score"))
    if count == 0 or not score:
        score = None  # Metacritic reports 0 for "tbd" (too few ratings for a score)
    return ScoreStats(score=score, review_count=count)


def parse_reviews(payload: dict[str, Any] | str, kind: str) -> ReviewPage:
    """`.../{critic|user}/games/{slug}/platform/{p}/web` review list response."""
    data = _unwrap(payload)
    items = data.get("items")
    if not isinstance(items, list):
        raise ParseError("reviews JSON has no items")
    reviews: list[ReviewData] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        quote = (item.get("quote") or "").strip()
        if not quote:
            continue
        source = item.get("publicationName") if kind == "critic" else item.get("author")
        platform = item.get("platform")
        if not platform:
            platform = (((item.get("reviewedProduct") or {}).get("platform")) or {}).get("name")
        reviews.append(
            ReviewData(
                kind=kind,
                quote=html_lib.unescape(quote),
                score=_to_float(item.get("score")),
                source=source or None,
                platform=platform,
                review_date=_parse_date(item.get("date")),
            )
        )
    return ReviewPage(reviews=reviews, total=_to_int(data.get("totalResults")) or len(reviews))


def absolute(url: str) -> str:
    return urljoin(BASE_URL, url)
