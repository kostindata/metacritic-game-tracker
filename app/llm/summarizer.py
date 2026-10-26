"""Likes / dislikes summaries of critic and user reviews via OpenRouter."""

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.llm.fingerprint import reviews_fingerprint
from app.logs import get_logger
from app.models import CrawlError, Game, LLMCall, Review, ReviewSummary

log = get_logger(__name__)

MAX_QUOTE_CHARS = 900
MIN_BULLETS, MAX_BULLETS = 3, 6
MIN_OVERVIEW_CHARS = 120
# Recorded with each summary for reference. Changing it does not regenerate existing
# summaries: they are only refreshed when their game gets new written reviews.
PROMPT_VERSION = 2

SYSTEM_PROMPT = """\
You are an experienced video game journalist summarising reviews of one game.
Reply with a JSON object only, shaped as:
{{"overview": "...", "likes": ["...", ...], "dislikes": ["...", ...]}}

- "overview": 3 to 5 sentences giving the overall consensus: how positive the reception is,
  what kind of game it is and who it suits, how it compares with earlier entries in the series
  or with similar games when reviewers mention it, and notable differences between platforms.
- "likes": 4 to 6 points about what reviewers praised most often.
- "dislikes": 4 to 6 points about what they criticised most often.
- Each point is one or two full sentences (roughly 20 to 40 words) and gives context:
  concrete examples, which game mode, mechanic or platform it concerns, and how widespread
  the opinion is ("most reviewers", "several players", "some critics").
- Use only information from the reviews provided. Merge duplicates. If criticism is rare,
  describe the minor complaints that do appear and say that they are minor.
- Write everything in {language}. Keep game, studio and platform names as they are.
"""


class SummaryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Summary:
    overview: str
    likes: list[str]
    dislikes: list[str]


def build_prompt(title: str, kind: str, reviews: list[Review]) -> str:
    who = "professional critics" if kind == "critic" else "players (user reviews)"
    lines = [f'Game: "{title}". Below are {len(reviews)} reviews by {who}.', ""]
    for i, r in enumerate(reviews, 1):
        quote = " ".join(r.quote.split())[:MAX_QUOTE_CHARS]
        score = f"{r.score:g}" if r.score is not None else "n/a"
        lines.append(f"[{i}] ({r.platform or 'unknown platform'}, score {score}) {quote}")
    lines += ["", "Return the JSON object now."]
    return "\n".join(lines)


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_summary(content: str) -> Summary:
    """Validate the model's JSON answer."""
    text = _FENCE_RE.sub("", content.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise SummaryError("no JSON object in LLM response")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SummaryError(f"invalid JSON from LLM: {exc}") from exc

    def clean(key: str) -> list[str]:
        values = data.get(key)
        if not isinstance(values, list):
            raise SummaryError(f"'{key}' is not a list")
        bullets = [" ".join(str(v).split()).lstrip("-•* ") for v in values if str(v).strip()]
        if len(bullets) < MIN_BULLETS:
            raise SummaryError(f"'{key}' has {len(bullets)} bullets, need {MIN_BULLETS}")
        return bullets[:MAX_BULLETS]

    overview = " ".join(str(data.get("overview") or "").split())
    if len(overview) < MIN_OVERVIEW_CHARS:
        raise SummaryError(f"'overview' is missing or too short ({len(overview)} chars)")
    return Summary(overview=overview, likes=clean("likes"), dislikes=clean("dislikes"))


class OpenRouterClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self.settings = settings
        self.http = http or httpx.Client(timeout=settings.llm_timeout_seconds)

    def summarise(self, title: str, kind: str, reviews: list[Review]) -> Summary:
        if not self.settings.openrouter_api_key:
            raise SummaryError("OPENROUTER_API_KEY is not set")
        body = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT.format(language=self.settings.summary_language)},
                {"role": "user", "content": build_prompt(title, kind, reviews)},
            ],
            "temperature": 0.3,
            "max_tokens": self.settings.llm_max_tokens,
            "reasoning": {"enabled": self.settings.llm_reasoning},
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "X-Title": "Metacritic Game Tracker",
        }
        last: Exception | None = None
        for attempt in range(1, self.settings.llm_max_attempts + 1):
            try:
                resp = self.http.post(f"{self.settings.llm_base_url}/chat/completions", json=body, headers=headers)
                if resp.status_code >= 400:
                    raise SummaryError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:300]}")
                payload = resp.json()
                if "error" in payload:
                    raise SummaryError(f"OpenRouter error: {payload['error']}")
                choice = payload["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise SummaryError(f"LLM answer truncated at max_tokens={self.settings.llm_max_tokens}")
                return parse_summary(choice["message"]["content"] or "")
            except (httpx.HTTPError, SummaryError, KeyError, IndexError, ValueError) as exc:
                last = exc
                log.warning("llm.attempt_failed", attempt=attempt, kind=kind, error=str(exc))
                if attempt < self.settings.llm_max_attempts:
                    time.sleep(2 ** attempt)
        raise SummaryError(str(last))


# On-demand generation: a summary is generated when someone opens the game, not when it is crawled.
RETRY_ERROR_AFTER = timedelta(minutes=1)
# Longer than the slowest live task (3 attempts x 120 s timeout + backoff ≈ 6 min), so a slow
# generation is not claimed a second time while it is still running.
STALE_PENDING_AFTER = timedelta(minutes=10)


def claim_for_generation(session: Session, game_id: int) -> list[str]:
    """Atomically mark the game's summaries that need an LLM run as pending.

    Picks summaries that were never generated or have new written reviews since the last one
    (both "outdated"), failed more than a minute ago, or pending for so long (10 minutes) that the
    task was probably lost. A ready summary ("ok") is never picked: it is reused until a crawl finds
    new reviews. The UPDATE ... RETURNING makes concurrent page views claim each summary
    only once. Returns the claimed kinds; the caller enqueues them.
    """
    now = func.now()
    needs_run = or_(
        ReviewSummary.status == "outdated",
        and_(ReviewSummary.status == "error", ReviewSummary.updated_at < now - RETRY_ERROR_AFTER),
        and_(ReviewSummary.status == "pending", ReviewSummary.updated_at < now - STALE_PENDING_AFTER),
    )
    stmt = (
        update(ReviewSummary)
        .where(ReviewSummary.game_id == game_id, needs_run)
        .values(status="pending", message=None, updated_at=now)
        .returning(ReviewSummary.kind)
    )
    return list(session.scalars(stmt))


def _load_reviews(session: Session, game_id: int, kind: str) -> list[Review]:
    return list(
        session.scalars(select(Review).where(Review.game_id == game_id, Review.kind == kind).order_by(Review.id))
    )


def generate_summary(sessions: sessionmaker[Session], llm: OpenRouterClient, game_id: int, kind: str) -> str:
    """Generate and store one summary; failures are recorded, not raised. Returns the status.

    The LLM call (up to minutes with retries) runs outside any transaction. The result is then
    written under a row lock, and only if the summary is still pending: a crawl may have changed
    it in the meantime (e.g. to "not_enough"). If the crawl replaced the reviews while the model
    was working, the new text is stored but marked "outdated", so the next view regenerates it
    from the current reviews instead of presenting a summary of the old set as up to date.
    """
    with sessions() as session:
        game = session.get(Game, game_id)
        summary = session.scalar(
            select(ReviewSummary).where(ReviewSummary.game_id == game_id, ReviewSummary.kind == kind)
        )
        if game is None or summary is None:
            return "missing"
        if summary.status != "pending":
            return summary.status  # superseded or already done
        title, slug = game.title, game.slug
        reviews = _load_reviews(session, game_id, kind)
    used_fingerprint = reviews_fingerprint(reviews)

    started = time.monotonic()
    try:
        result: Summary | None = llm.summarise(title, kind, reviews)
        error = None
    except Exception as exc:  # noqa: BLE001
        result, error = None, exc
    duration_ms = int((time.monotonic() - started) * 1000)

    with sessions.begin() as session:
        summary = session.scalar(
            select(ReviewSummary)
            .where(ReviewSummary.game_id == game_id, ReviewSummary.kind == kind)
            .with_for_update()
        )
        if error is not None:
            session.add(LLMCall(game_id=game_id, kind=kind, ok=False, error=str(error)[:2000], duration_ms=duration_ms))
            session.add(CrawlError(game_slug=slug, stage="llm", message=f"{kind} summary: {error}"[:2000]))
            log.warning("llm.summary_failed", game_id=game_id, kind=kind, error=str(error))
            if summary is None or summary.status != "pending":
                return summary.status if summary else "missing"
            summary.status = "error"
            summary.message = str(error)[:1000]
            return "error"

        assert result is not None
        session.add(LLMCall(game_id=game_id, kind=kind, ok=True, duration_ms=duration_ms))
        if summary is None or summary.status != "pending":
            log.info("llm.summary_discarded", game_id=game_id, kind=kind,
                     status=summary.status if summary else None)
            return summary.status if summary else "missing"
        current = reviews_fingerprint(_load_reviews(session, game_id, kind))
        summary.status = "ok" if current == used_fingerprint else "outdated"
        summary.overview = result.overview
        summary.likes, summary.dislikes = result.likes, result.dislikes
        summary.prompt_version = PROMPT_VERSION
        summary.message = None
        summary.review_count = len(reviews)
        summary.reviews_fingerprint = used_fingerprint
        summary.summary_generated_at = datetime.now(timezone.utc)
    log.info("llm.summary_generated", game_id=game_id, kind=kind, reviews=len(reviews),
             reviews_changed=summary.status == "outdated")
    return summary.status
