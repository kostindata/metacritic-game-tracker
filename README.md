# Metacritic Game Tracker

A service that crawls Metacritic's Games section every hour, stores game data in PostgreSQL,
generates AI "likes / dislikes" summaries of critic and user reviews, and serves a web UI with a
real-time monitoring dashboard.

- **Python 3.12 · FastAPI · PostgreSQL (SQLAlchemy 2 + Alembic) · Redis · Celery + Beat**
- **httpx** for all pages (Metacritic is server-rendered); **Playwright/Chromium** as an automatic
  fallback when a page arrives without its server-rendered payload
- **Jinja2 + HTMX** UI; dashboard pushed over **Server-Sent Events**
- LLM: `deepseek/deepseek-v4.1-flash` via OpenRouter (`OPENROUTER_API_KEY`)
- Interface in **Russian**; all times, schedules and the crawl day use **UTC+3** (`TIMEZONE=Europe/Moscow`)

### Page speed

- Game pages and lists are rendered server-side in about 30 ms.
- No third-party CDNs in the critical path: htmx, its SSE extension and the Onest font
  (Cyrillic and Latin subsets) are served by the app from `app/web/static/` and scripts load
  with `defer`. Static URLs carry a content hash (`static_url()`) and are sent with
  `Cache-Control: max-age=31536000, immutable`, so repeat visits load nothing but the HTML. After a
  change, the hash changes and browsers fetch the new file.
- The trailer shows its poster with a play button; the JW Player iframe and its scripts load only
  on click.
- The main cover image loads with high priority. Covers in lists and carousels load lazily.

### Localisation and timezone

- The UI (all templates, statuses, run notes, error stages, relative times) is in Russian. The JSON
  endpoints, logs and stored values stay in English; labels are mapped at render time (`LABELS` in
  `app/web/main.py`). Data from Metacritic (titles, platforms, genres, descriptions) is shown as is.
- `TIMEZONE` (default `Europe/Moscow`, permanently UTC+3) drives the crawl day, Celery Beat's
  crontab entries (Celery's `timezone` setting), "next run" / "next reset" on the dashboard and every
  displayed timestamp (`25.10.2026 17:00:00 (UTC+3)`). Timestamps are stored in UTC.
- Default sorting everywhere is newest first (release date, descending; undated games last): the game
  list, the New Releases carousel, and the similar games (picked by similarity, shown newest first).
- Covers without an image, or whose image fails to load, show a "TBD" placeholder.

## Quick start

```bash
cp .env.example .env        # then set OPENROUTER_API_KEY
docker compose up --build
```

Open <http://localhost:8000> (game list) and <http://localhost:8000/dashboard> (monitoring).
The first crawl runs at the top of the next hour; press **Run crawl now** to start one immediately.

Without an API key, crawling works normally; summaries are recorded as LLM errors and retried on
later crawls once a key is configured.

> **"all predefined address pools have been fully subnetted"?** A VPN with catch-all routes
> (e.g. `0.0.0.0/1` + `128.0.0.0/1`) overlaps Docker's address pools, so Docker cannot pick a
> subnet. Run `cp docker-compose.override.example.yml docker-compose.override.yml` (Compose loads it
> automatically) to give the network an explicit subnet; change the subnet if it clashes with
> another Docker network.

### Services (`docker-compose.yml`)

| Service        | Role                                                                    |
|----------------|-------------------------------------------------------------------------|
| `postgres`     | Game data, crawl state, run history, errors                             |
| `redis`        | Celery broker, crawl lock, rate limiter, worker heartbeats, SSE pub/sub |
| `migrate`      | One-shot `alembic upgrade head`; the other app services wait for it     |
| `web`          | FastAPI + Jinja2/HTMX UI, SSE stream (port `8000`)                      |
| `worker-crawl` | Celery worker on queue `crawl`, 2 processes (see Concurrency)           |
| `worker-llm`   | Celery worker on queue `llm`, concurrency 2 (on-demand AI summaries)    |
| `beat`         | Celery Beat: hourly crawl, daily reset at 00:00 UTC+3, orphaned-run check every minute |

### Running tests

```bash
docker compose run --rm --no-deps web pytest
```

The unit tests need no DB or network. They cover the parsers against saved Metacritic HTML/JSON
fixtures in `tests/fixtures/`, the cursor walk and daily-reset logic, similarity scoring, summary
regeneration rules, LLM output validation and schedule computation.

`tests/test_db_integration.py` runs against a real, disposable PostgreSQL database and is skipped
unless `TEST_DATABASE_URL` is set: game upserts, the summary lifecycle (claim once, reuse, new
reviews, reviews changing during generation, LLM failures), the similar-games refresh and the web
endpoints. Its tables are truncated between tests:

```bash
docker compose exec postgres createdb -U mc mc_test
docker compose run --rm --no-deps -e DATABASE_URL=postgresql+psycopg://mc:mc@postgres:5432/mc_test web alembic upgrade head
docker compose run --rm --no-deps -e TEST_DATABASE_URL=postgresql+psycopg://mc:mc@postgres:5432/mc_test \
  -e OPENROUTER_API_KEY= -e REDIS_URL=redis://redis:6379/15 web pytest
```

### Useful endpoints

| Method | Path                | Description                                                       |
|--------|---------------------|-------------------------------------------------------------------|
| GET    | `/`                 | New Releases carousel + game list (`q`, `platform`, `sort=release\|metascore\|userscore\|title` (default `release`), `order=desc\|asc` (default `desc`), `page`) |
| GET    | `/games/{slug}`     | Game detail; queues missing/outdated AI summaries                 |
| GET    | `/games/{slug}/summaries` | Summary block only (polled while generating)                |
| POST   | `/runs`             | Manual crawl. JSON: `202 {"status":"started"}` or `409 {"status":"already_running"}`; cross-site posts get `403` |
| GET    | `/dashboard`        | Monitoring dashboard                                              |
| GET    | `/dashboard/stream` | SSE stream of dashboard sections                                  |
| GET    | `/api/dashboard`    | Dashboard data as JSON                                            |

## Architecture

```
                      ┌──────────── Redis ─────────────┐
 Browser ── HTTP ──►  │ broker · lock · rate limiter    │
   ▲   ◄── SSE ─── web│ worker heartbeats · pub/sub     │
   │                  └──▲───────────▲────────────▲────┘
   │                     │           │            │
   │              worker-crawl   worker-llm      beat ── hourly: scheduled_crawl
   │                  │   │           │                  00:00:  daily_reset
   │      httpx (≤1 rps)  │           │ OpenRouter
   │      Metacritic ◄────┘           │
   └──────────── PostgreSQL ◄─────────┘
```

### Code layout

```
app/
  config.py            settings (env vars, see .env.example)
  logs.py              structlog JSON logging (stdlib + Celery routed through it)
  models.py            SQLAlchemy models          alembic/  migrations
  scraper/
    nuxt.py            decoder for Nuxt's __NUXT_DATA__ (devalue) payload
    parsers.py         pure parsers: New Releases, browse page, game page, review/score JSON
    client.py          rate-limited, retrying HTTP client (+ Playwright fallback)
  crawler/
    cursor.py          pure cursor / daily-reset / listing-walk logic
    runner.py          a crawl run: walk listing, fetch, upsert, per-game error isolation
    upsert.py          game upsert, reviews, summary regeneration decision
    service.py         run reservation (lock + run row) shared by web and Beat
  llm/summarizer.py    OpenRouter call, prompt, JSON validation
  similarity.py        similar-games scoring
  monitoring.py        crawl lock, worker heartbeats, change notifications
  worker.py, tasks.py  Celery app, signals, tasks
  schedule.py          Beat schedule + "next run" computation
  web/                 FastAPI app, queries, templates, static
```

### What is fetched

Metacritic is a Nuxt app whose pages are server-rendered, so plain HTTP is enough:

| Data                                    | Source                                                              |
|-----------------------------------------|---------------------------------------------------------------------|
| New Releases (first run of the day)     | `https://www.metacritic.com/game/` – `New Releases` carousel (HTML)  |
| Listing (later runs)                    | `https://www.metacritic.com/browse/game/all/all/all-time/new/?page=N` (24 games per page) |
| Title, description, cover, trailer, developers, publishers, genres, per-platform Metascore / critic count / release date, lead platform's user score | Game page `/game/{slug}/` – `__NUXT_DATA__` payload (with JSON-LD as a fallback for some fields) |
| User score & count for other platforms  | `backend.metacritic.com/reviews/metacritic/user/games/{slug}/platform/{p}/stats/web` |
| Up to 30 critic + 30 user reviews       | `backend.metacritic.com/reviews/metacritic/{critic\|user}/games/{slug}/platform/{p}/web` (paged, platforms with the most reviews first) |

These are the JSON endpoints the Metacritic site itself calls; they count against the same rate
limit. Cover images are stored as URLs (not downloaded). The video link is the trailer's JW Player
embed URL, or `null` when there is no trailer.

### Crawl logic

**Cursor.** A single `crawl_state` row holds `(day, phase, page, position)`:
`phase=new_releases, position=i` is the i-th New Releases card; `phase=browse, page=p, position=i`
is the i-th card of browse page *p*. The listing is walked lazily (a page is fetched only when
reached), and **every examined item advances the cursor and is persisted immediately** —
processed, failed, or skipped as already done today. So the next run (scheduled or manual) resumes
exactly after the last item looked at, a crash loses at most one game, and when a run stops in the
middle of a page the rest of the page carries over to the next run.

**Per run.** Collect up to `GAMES_PER_RUN` (500) games not yet processed today:

1. Fresh day → cursor = New Releases #1. The run takes the section's first `NEW_RELEASES_COUNT`
   (20) games (these also form the UI carousel); after the last one the cursor moves to browse
   page 1, and the same run continues there until it has 500 games.
2. Later runs continue on the browse listing (newest first) from the cursor.
3. Games already in `processed_today` (or already seen in this walk) are skipped. Because the
   listing is newest-first, new releases push older items down; the position cursor may then
   re-see some items, but never misses any, and re-seen items are skipped cheaply.
4. The run stops after 500 attempted games, when the listing runs out (empty page), or after
   `MAX_LISTING_PAGES_PER_RUN` (100) listing pages (500 games need about 21 pages of 24).

**Concurrency and run length.** Within a run, the listing is walked in order on one thread,
while up to `CRAWL_CONCURRENCY` (4) games are fetched and stored in parallel by a thread pool. All
their requests share the global Redis rate limiter, so total traffic stays at most 1 request per
second. Concurrency only fills the slots a single game would leave idle while waiting for slow
responses (game pages take ~1.2 s to arrive), retries or extra review pages.
- **Cursor stays exact.** Games finish out of order, so `OrderedProgress` (`cursor.py`) advances
  the saved cursor only past a contiguous prefix of finished games. After a stop or crash the next
  run resumes at the first unfinished game, and games finished after it are skipped as already
  processed today.
- **One request for most games.** The game page already contains the lead platform's user score,
  including "no ratings yet", so no separate stats request is made for it. A new game with one
  platform and no reviews costs a single request.
- **Measured** on the same machine: about 1.4 s per game instead of about 7.3 s, roughly 5× faster
  (169 games in 4 minutes) at 0.65 requests/second. A 500-game run takes roughly 12–15 minutes
  instead of an hour; games with many platforms and reviews cost more requests.
- On a clean stop, the games in flight are finished before the run closes (a few seconds, well
  within `stop_grace_period: 90s`).

**Daily reset.** Beat runs `daily_reset` at `DAILY_RESET_HOUR` local time (default 00:00 UTC+3,
i.e. 21:00 UTC): it clears
older `processed_today` rows and sets the cursor back to New Releases #1. The same check also runs
at the start of every crawl, so a missed or delayed Beat tick can never leak yesterday's cursor into
today. The reset is idempotent.

**Upsert.** Games are keyed by their Metacritic URL slug: `INSERT … ON CONFLICT (slug) DO UPDATE`,
with `xmax = 0` telling inserts from updates. `first_seen_at` is set only on insert;
`last_crawled_at` on every crawl. Platforms are synced (added/updated/removed); the game also stores
its best Metascore / User Score across platforms for sorting.

**Concurrency.** A Redis lock (`SET NX EX` with an owner token) allows a single active run. The
running crawl refreshes it before every Metacritic request and after every listing item, so the TTL
can stay short (`LOCK_TTL_SECONDS`, 120 s). If a worker is killed mid-run, its lock expires within
two minutes. The dashboard flags the orphaned run as "worker lost", and the next run that takes the
lock closes it as `aborted`. Manual and
scheduled triggers take the lock *before* anything is queued: `POST /runs` either returns
"started" (lock taken, run row created, task queued) or "already running" (`409`). A Beat tick that
finds the lock taken logs and skips. Both share the same cursor. The crawl worker has two
processes, so a schedule tick that fires during a run is handled at once (and skipped) by the idle
process instead of waiting in the queue and starting a back-to-back run. Beat ticks also expire
after 15 minutes, so ticks never pile up while workers are down.

**Stopping and restarts.** Restarting containers mid-run (`docker compose down/up`, rebuilds) is
safe:
- On SIGTERM the crawl worker's `worker_shutting_down` signal sets a stop flag in Redis. The run
  finishes the game in progress, saves the cursor, marks itself `aborted` ("worker stopped") and
  releases the lock. `stop_grace_period: 90s` on `worker-crawl` gives it time to do so.
- If a worker is killed hard, a Beat task (`cleanup_runs`, every minute) takes the lock as soon as
  the dead worker's lock expires and closes the orphaned run. That happens within about 3 minutes,
  and the dashboard shows "воркер не отвечает" meanwhile. Taking the lock first means it can never
  close a run that is legitimately starting.
- Either way the next run (hourly, or «Запустить сбор») continues from the saved cursor.

**Politeness & robustness.** A Redis-backed limiter (Lua script on the Redis clock) spaces *all*
Metacritic requests, from any process, ≥ 1 s apart. Requests use a realistic desktop Chrome
User-Agent. Network errors, 408/425/429 and 5xx are retried up to 4 times with exponential backoff
(2 s, 4 s, 8 s + jitter, honouring `Retry-After`); 404 is not retried. Each game is processed in its
own try/except and DB transaction (a platform whose user-score request fails keeps its stored
score): a failing game is recorded in `crawl_errors` (shown on the
dashboard) and the run continues. If only review or user-score fetches fail, the game is still saved
and the partial failure is recorded. A listing-page failure ends the run with status `failed`, and
the cursor stays where it was.

### Review summaries

- Up to `MAX_REVIEWS_PER_TYPE` (30) critic and 30 user reviews are fetched per game, across all
  platforms (platforms with the most reviews first) and stored in `reviews`.
- If fewer than 3 written reviews of a type could be fetched, the summary is stored as **"Not
  enough reviews yet"** (shown as «Пока недостаточно отзывов») and the LLM is not called.
- **Generated once, then cached.** A summary is generated only:
  1. the first time someone opens the game's page, or
  2. on the next page open after a crawl found **new written reviews** for that game.

  Every other view, from any visitor, reuses the stored summary without calling the LLM.
- **"New reviews" is exact.** Each summary stores `reviews_fingerprint`, a SHA-256 over the
  written reviews it was built from (platform, source, date and normalised text; order does not
  matter; `app/llm/fingerprint.py`). After fetching a game's reviews, a crawl compares the new
  fingerprint with the stored one. Only a different set marks the summary `outdated`, and it
  keeps showing the old text until it is regenerated. New star ratings without text, which change
  Metacritic's rating counts all the time, do not trigger a regeneration.
- Opening a game's page runs `claim_for_generation`: one atomic `UPDATE … RETURNING` marks as
  `pending` only the summaries that are `outdated` (never generated, or new reviews), failed more
  than a minute ago, or stuck in `pending` for over 10 minutes (lost task; longer than the slowest live generation,
  so a slow one is never claimed twice). A ready (`ok`) summary
  is never claimed. Each claimed summary is queued as a `summarize_game` task on the `llm` queue.
  Concurrent views of the same game claim it only once. Without `OPENROUTER_API_KEY` nothing is
  queued and the page says AI summaries are disabled.
- While a summary is pending, the page's summary block shows a progress indicator and re-fetches
  itself (`GET /games/{slug}/summaries`, HTMX polling every 2 s) until it is ready. The rest of
  the page is not reloaded. Each poll runs the same claim, so a lost task is re-queued.
- The LLM call runs outside any database transaction. Its result is written only if the summary
  is still `pending`; if a crawl replaced the reviews while the model was working, the text is
  stored as `outdated` and regenerated from the current reviews on the next poll or view.
- Games nobody opens never cost an LLM call. The first visitor waits for the generation (median
  ≈7 s; critic and user summaries run in parallel), and later visitors get the stored summary
  instantly. The model's hidden reasoning is off by default (`LLM_REASONING=false`). On the same
  prompts that measured a median of 6.6 s instead of 19.4 s, with points of about 20 instead of
  25 words.
- Every summary is labelled **«Сгенерировано ИИ»**, with a footnote giving the number of reviews it
  is based on and the generation time. The model name is not shown in the UI.
- The model writes in `SUMMARY_LANGUAGE` (default Russian) and returns JSON
  `{"overview": "...", "likes": [...], "dislikes": [...]}`:
  - an overview of 3–5 sentences: overall reception, who the game suits, comparisons with earlier
    entries or similar games, and platform differences mentioned by reviewers;
  - 4–6 likes and 4–6 dislikes, each one or two full sentences with context (concrete examples, the
    mode, mechanic or platform concerned, how widespread the opinion is).

  The answer is validated (3–6 points per list, non-trivial overview) and retried up to 3 times. On
  success `summary_generated_at` and `prompt_version` are set. Each call is logged in `llm_calls`,
  which feeds the dashboard's totals.
- `PROMPT_VERSION` (`app/llm/summarizer.py`) is recorded with each summary for reference. Changing
  the prompt does not regenerate existing summaries; they refresh only when their game gets new
  written reviews.

### New Releases carousel

The game list page opens with a carousel of the New Releases games. The first time a crawl day
fetches the New Releases section, its list is stored in `new_release_entries` (day, position, slug).
Later fetches the same day don't replace it, so the carousel stays stable all day. The carousel
shows the most recent day's list in Metacritic's order, limited to games already in the database.
The full, filterable, paginated list below it includes these games as well.

### Similar games

For the game being viewed (*A*) and each candidate (*B*), SQL pre-filters to games sharing at least
one genre, developer or platform. Then:

```
similarity(A, B) = 0.50 · J(genres_A, genres_B)
                 + 0.20 · J(developers_A, developers_B)
                 + 0.20 · J(platforms_A, platforms_B)
                 + 0.10 · closeness(release_A, release_B)

J(X, Y)          = |X ∩ Y| / |X ∪ Y|          (Jaccard; 0 if either set is empty;
                                                names compared case-insensitively)
closeness(a, b)  = max(0, 1 − |a − b| in days / 730)   (0 if a date is unknown)
```

Scores fall in [0, 1]. Genres carry the most weight. The release-date term only breaks ties
between otherwise similar games, so a shared date alone never qualifies a game. Implemented in
`app/similarity.py`.

**Where they appear.** A game's page shows its top 6 similar games as cards, each opening that
game's page.

**Precomputed.** Similar games are computed for the whole database after every crawl run
(`refresh_similar_games` task on the crawl worker's second process) and stored in
`game_similar` (top 6 per game). Game pages read that table instead of computing on every view. To avoid comparing every pair, candidates come from an inverted index of shared genres and
developers. When a game has fewer than 18 such candidates, the 300 most recent games sharing a
platform are added, so games with rare genres still get suggestions. About 2,000 games refresh in
roughly 8 seconds. A game crawled since the last refresh gets its similar games computed live on
its page.

### Monitoring dashboard

`/dashboard` subscribes to `/dashboard/stream` (HTMX SSE extension). The server re-renders each
section (workers, current run, today, totals, history, errors) and pushes only the sections whose
HTML changed. It wakes immediately on Redis pub/sub notifications sent by workers (run progress,
worker status, LLM results), and otherwise every 2 s.

- **Workers**: each Celery worker process (and Beat) writes a heartbeat key with a 30 s TTL every
  10 s. Celery signals set its status: `busy` (`task_prerun`), `idle` (`task_postrun`), or `error`
  (`task_failure`, kept until the next task). Busy crawl workers also report the current game. A
  dead worker disappears when its key expires.
- **Current run**: started at, trigger (scheduled/manual), progress *X of 500*, current game.
- **Today**: unique games processed, inserted vs updated, failures, runs, cursor position.
- **Totals**: games in DB, LLM summaries generated, LLM errors, summaries pending.
- **Next scheduled run** and next daily reset, from the same settings Beat uses.
- **Recent errors**: the last 20, with run, game, stage and message.
- **Run history**: the last 10 runs with duration, counts and status
  (`success`, `partial` (some games failed), `failed`, `aborted`).

### Logging

All processes log one JSON object per line via `structlog`, with stdlib, Celery and Uvicorn logs
routed through the same renderer. Events are named (`crawl.run_started`, `crawl.game_done`,
`crawl.game_failed`, `http.retryable_status`, `llm.summary_generated`, …) and carry context such as
`run_id`, `slug` and `attempt`. Set `LOG_FORMAT=console` for human-readable output.

## Configuration

All settings are environment variables; see `.env.example`. The main ones:

| Variable                  | Default                          | Meaning                                    |
|---------------------------|----------------------------------|--------------------------------------------|
| `OPENROUTER_API_KEY`      | –                                | OpenRouter key for summaries               |
| `LLM_MODEL`               | `deepseek/deepseek-v4.1-flash`   | Model id                                   |
| `GAMES_PER_RUN`           | `500`                            | New games per run                          |
| `NEW_RELEASES_COUNT`      | `20`                             | Games taken from New Releases (carousel)   |
| `MAX_LISTING_PAGES_PER_RUN` | `100`                          | Safety cap on listing pages per run        |
| `MAX_REVIEWS_PER_TYPE`    | `30`                             | Critic / user reviews fetched per game     |
| `MIN_REVIEWS_FOR_SUMMARY` | `3`                              | Below this: "Not enough reviews yet"       |
| `CRAWL_SCHEDULE_MINUTE`   | `0`                              | Hourly crawl at HH:MM                      |
| `TIMEZONE`                | `Europe/Moscow` (UTC+3)          | Crawl day, schedules, displayed times      |
| `DAILY_RESET_HOUR`        | `0`                              | Local hour at which "today" rolls over     |
| `SUMMARY_LANGUAGE`        | `Russian`                        | Language of the AI summaries               |
| `REQUEST_INTERVAL_SECONDS`| `1.0`                            | Min spacing between Metacritic requests    |
| `PLAYWRIGHT_ENABLED`      | `true`                           | Headless-browser fallback                  |
| `LOG_FORMAT`              | `json`                           | `json` or `console`                        |

## Development without Docker

```bash
pip install -e ".[dev]"
playwright install chromium            # optional fallback
export DATABASE_URL=postgresql+psycopg://mc:mc@localhost:5432/mc REDIS_URL=redis://localhost:6379/0
alembic upgrade head
uvicorn app.web.main:app --reload
WORKER_NAME=crawl celery -A app.worker worker -Q crawl -c 1
WORKER_NAME=llm   celery -A app.worker worker -Q llm -c 2
celery -A app.worker beat
pytest
```
