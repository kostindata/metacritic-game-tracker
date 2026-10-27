import asyncio
import hashlib
import mimetypes
import re
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import covers
from app.config import get_settings
from app.crawler.service import trigger_manual_run
from app.db import get_db, session_scope
from app.llm.on_demand import request_summaries
from app.logs import configure_logging, get_logger
from app.monitoring import EVENTS_CHANNEL
from app.web import queries

configure_logging()
log = get_logger(__name__)

# Slim base images lack these in the MIME table; fonts would be served as text/plain.
mimetypes.add_type("font/woff2", ".woff2")

BASE_DIR = Path(__file__).parent
app = FastAPI(title="Metacritic Game Tracker")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@lru_cache
def _static_url(path: str) -> str:
    """URL of a static file with a content hash, so it can be cached "forever" and still
    change the moment the file does."""
    digest = hashlib.sha1((BASE_DIR / "static" / path).read_bytes()).hexdigest()[:10]
    return f"/static/{path}?v={digest}"


templates.env.globals["static_url"] = _static_url


@app.middleware("http")
async def static_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        versioned = "v" in request.query_params or request.url.path.startswith("/static/vendor/")
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if versioned and response.status_code == 200 else "no-cache"
        )
    return response

DASHBOARD_SECTIONS = ("workers", "run", "today", "totals", "errors", "history")


# --------------------------------------------------------------------------- template helpers


# Display labels for values stored in English (statuses, stages, run notes).
LABELS = {
    # worker / run status
    "idle": "свободен", "busy": "занят", "error": "ошибка",
    "queued": "в очереди", "running": "выполняется", "success": "успешно",
    "partial": "частично", "failed": "сбой", "aborted": "прерван",
    # triggers
    "manual": "вручную", "scheduled": "по расписанию",
    # error stages
    "listing": "список игр", "game": "страница игры", "store": "сохранение", "llm": "ИИ-сводка",
    "user_stats": "оценки игроков", "critic_reviews": "рецензии критиков",
    "user_reviews": "отзывы игроков", "run": "запуск", "cover": "обложка",
    # worker names
    "crawl": "сбор данных", "beat": "планировщик",
    # run notes
    "listing exhausted": "список игр закончился",
    "worker lost (lock expired)": "воркер был остановлен аварийно; следующий запуск продолжит с этого места",
    "superseded (worker crashed?)": "заменён новым запуском (воркер упал?)",
    "lock expired before the run started": "блокировка истекла до старта",
    "crawl lock lost": "блокировка потеряна",
    "worker stopped": "остановлен вместе с воркером; следующий запуск продолжит с этого места",
}


def _label(value):
    if value is None:
        return "—"
    return LABELS.get(str(value), value)


def _tz_name(value: datetime) -> str:
    offset = value.utcoffset()
    hours = int(offset.total_seconds() // 3600) if offset else 0
    return f"UTC{hours:+d}" if hours else "UTC"


def _fmt_dt(value: datetime | None) -> str:
    """Date and time in the app's timezone (UTC+3 by default)."""
    if value is None:
        return "—"
    local = value.astimezone(get_settings().tz)
    return f"{local:%d.%m.%Y %H:%M:%S} ({_tz_name(local)})"


def _fmt_date(value) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


def _ago(value: datetime | float | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, tz=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - value).total_seconds())
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        text = f"{seconds} с"
    elif seconds < 3600:
        text = f"{seconds // 60} мин"
    elif seconds < 86400:
        text = f"{seconds // 3600} ч {seconds % 3600 // 60} мин"
    else:
        text = f"{seconds // 86400} дн."
    return f"через {text}" if future else f"{text} назад"


def _duration(run) -> str:
    # A run closed after its worker died has no real end time: the gap is mostly dead time.
    if not run.started_at or not run.finished_at or run.note == "worker lost (lock expired)":
        return "—"
    seconds = int((run.finished_at - run.started_at).total_seconds())
    if seconds >= 3600:
        return f"{seconds // 3600} ч {seconds % 3600 // 60:02d} мин"
    return f"{seconds // 60} мин {seconds % 60:02d} с"


_JW_PLAYER_RE = re.compile(r"^https://cdn\.jwplayer\.com/players/([A-Za-z0-9]+)(?:-[A-Za-z0-9]+)?\.html")


def _jw_media_id(url: str | None) -> str | None:
    """Media id of a JW Player embed URL (used for its poster image), or None."""
    match = _JW_PLAYER_RE.search(url or "")
    return match.group(1) if match else None


def _cover_src(game) -> str | None:
    """The local copy of a game's cover when there is one, else the Metacritic URL."""
    cover_file = getattr(game, "cover_file", None)
    return f"/covers/{cover_file}" if cover_file else game.cover_url


def _ucfirst(value) -> str:
    """Capitalise the first letter only (Jinja's `capitalize` lowercases the rest: "ИИ" -> "Ии")."""
    text = str(value)
    return text[:1].upper() + text[1:]


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural form: 1 игра, 2 игры, 5 игр."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _score_class(value, kind: str = "meta") -> str:
    if value is None:
        return "score-none"
    v = value * 10 if kind == "user" else value
    return "score-good" if v >= 75 else "score-mixed" if v >= 50 else "score-bad"


templates.env.filters.update(
    dt=_fmt_dt, d=_fmt_date, ago=_ago, duration=_duration, score_class=_score_class, label=_label, plural=_plural, ucfirst=_ucfirst, jw_media_id=_jw_media_id,
    cover_src=_cover_src,
)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _wants_html(request: Request) -> bool:
    return not request.url.path.startswith(("/api/", "/runs", "/static/")) and (
        "text/html" in request.headers.get("accept", "") or _is_htmx(request)
    )


@app.exception_handler(StarletteHTTPException)
async def html_http_errors(request: Request, exc: StarletteHTTPException):
    """Browsers get a page in the site's layout instead of raw JSON; API clients keep JSON."""
    if not _wants_html(request):
        return await http_exception_handler(request, exc)
    message = "Игра не найдена" if exc.status_code == 404 and request.url.path.startswith("/games/") else (
        "Страница не найдена" if exc.status_code == 404 else "Что-то пошло не так"
    )
    return templates.TemplateResponse(
        request, "error.html", {"status": exc.status_code, "message": message}, status_code=exc.status_code
    )


# --------------------------------------------------------------------------- pages


SORT_VALUES = ("release", "metascore", "userscore", "title")


@app.get("/", response_class=HTMLResponse)
def game_list(
    request: Request,
    q: str | None = None,
    platform: str | None = None,
    sort: str = "release",
    order: str = "desc",
    page: str = "1",
    db: Session = Depends(get_db),
):
    # Hand-edited or stale links fall back to the defaults instead of a validation error.
    sort = sort if sort in SORT_VALUES else "release"
    order = order if order in ("asc", "desc") else "desc"
    page_no = int(page) if page.strip().isdigit() else 1
    q = (q or "").strip()[:200] or None
    result = queries.list_games(db, q=q, platform=platform or None, sort=sort, order=order, page=page_no)
    context = {
        "result": result,
        "platforms": queries.all_platforms(db),
        "new_releases": queries.new_release_games(db),
        "filters": {"q": q or "", "platform": platform or "", "sort": sort, "order": order},
    }
    template = "_game_grid.html" if _is_htmx(request) and request.headers.get("HX-Target") == "results" else "games.html"
    return templates.TemplateResponse(request, template, context)


@app.get("/games/{slug}", response_class=HTMLResponse)
def game_detail(request: Request, slug: str, db: Session = Depends(get_db)):
    game = queries.get_game(db, slug)
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found")
    # AI summaries are generated on demand: opening the game queues the ones that are missing
    # or outdated, and the summary block polls until they are ready.
    request_summaries(game.id)
    db.expire(game, ["summaries"])
    return templates.TemplateResponse(
        request,
        "game_detail.html",
        {**_summary_context(game), "similar": queries.newest_first(queries.similar_for_page(db, game))},
    )


@app.get("/games/{slug}/summaries", response_class=HTMLResponse)
def game_summaries(request: Request, slug: str, db: Session = Depends(get_db)):
    """The summary block alone; polled by the detail page while a summary is being generated."""
    game = queries.get_game(db, slug)
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found")
    # Re-claims a generation whose task was lost, and one that finished on reviews a crawl has
    # replaced meanwhile, so the block keeps polling until an up-to-date summary is shown.
    if request_summaries(game.id):
        db.expire(game, ["summaries"])
    return templates.TemplateResponse(request, "_summaries.html", _summary_context(game))


def _summary_context(game) -> dict:
    settings = get_settings()
    summaries = {s.kind: s for s in game.summaries}
    return {
        "game": game,
        "summaries": summaries,
        "generating": any(s.status == "pending" for s in summaries.values()),
        "ai_enabled": bool(settings.openrouter_api_key),
    }


@app.post("/runs")
def trigger_run(request: Request):
    # No auth by design; still refuse cross-site form posts (a page elsewhere starting crawls).
    if request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse({"status": "forbidden"}, status_code=403)
    try:
        result = trigger_manual_run()
    except Exception as exc:  # noqa: BLE001 - Redis/broker down: say so instead of a bare 500
        log.exception("crawl.trigger_failed")
        if _is_htmx(request):
            return templates.TemplateResponse(request, "_run_trigger.html", {"result": None})
        return JSONResponse({"status": "error", "error": str(exc)[:300]}, status_code=503)
    status_code = 202 if result.started else 409
    if _is_htmx(request):
        return templates.TemplateResponse(
            request, "_run_trigger.html", {"result": result}, status_code=200
        )
    return JSONResponse(
        {"status": "started" if result.started else "already_running", "run_id": result.run_id},
        status_code=status_code,
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    snap = queries.dashboard_snapshot(db, get_settings())
    return templates.TemplateResponse(request, "dashboard.html", {"s": snap})


def _render_sections() -> dict[str, str]:
    with session_scope() as db:
        snap = queries.dashboard_snapshot(db, get_settings())
        return {
            name: templates.get_template(f"dash/_{name}.html").render(s=snap)
            for name in DASHBOARD_SECTIONS
        }


@app.get("/dashboard/stream")
async def dashboard_stream(request: Request):
    """Server-Sent Events: pushes re-rendered dashboard sections when they change.

    Wakes immediately on Redis notifications from workers, and every 2 s otherwise
    (so heartbeats, countdowns and expired workers are reflected too).
    """

    async def events():
        client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
        pubsub = client.pubsub()
        last: dict[str, str] = {}
        try:
            await pubsub.subscribe(EVENTS_CHANNEL)
            while not await request.is_disconnected():
                try:
                    sections = await run_in_threadpool(_render_sections)
                except Exception as exc:  # noqa: BLE001
                    log.warning("sse.render_failed", error=str(exc))
                    sections = {}
                for name, html in sections.items():
                    if last.get(name) != html:
                        last[name] = html
                        yield {"event": name, "data": html}
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2.0)
                    if message:
                        await asyncio.sleep(0.3)  # coalesce bursts of notifications
                        while await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.01):
                            pass
                except Exception:  # noqa: BLE001 - Redis hiccup: fall back to polling
                    await asyncio.sleep(2.0)
        finally:
            try:
                await pubsub.unsubscribe(EVENTS_CHANNEL)
                await pubsub.aclose()
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass

    return EventSourceResponse(events(), ping=15)


@app.get("/api/dashboard")
def dashboard_json(db: Session = Depends(get_db)):
    s = queries.dashboard_snapshot(db, get_settings())
    run = s["current"]
    return {
        "workers": s["workers"],
        "current_run": None if run is None else {
            "id": run.id, "trigger": run.trigger, "status": run.status, "started_at": run.started_at,
            "progress": run.processed + run.failed, "target": run.target, "current_game": run.current_game,
        },
        "today": {**s["today"], "day": str(s["today"]["day"])},
        "totals": s["totals"],
        "next_run": s["schedule"]["next_run"],
    }


@app.get("/covers/{name}")
def cover_file(name: str):
    """Stored cover image. Names are content-addressed (hash of the source URL), so they never
    change meaning and can be cached for a year."""
    path = Path(get_settings().covers_dir) / name
    if not covers.FILE_RE.match(name) or not path.is_file():
        raise HTTPException(status_code=404, detail="Cover not found")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/healthz")
def healthz():
    return {"ok": True}
