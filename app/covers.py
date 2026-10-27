"""Local copies of cover images.

Covers are downloaded during the crawl (through the same rate limiter as every other Metacritic
request) and served by the app from /covers/<file>. A file is named after the SHA-1 of its
source URL, so an unchanged cover is never downloaded twice and a changed one gets a new name
(browsers can cache it forever). Pages fall back to the Metacritic URL when there is no copy.

Backfill covers of games crawled before this existed:
    docker compose exec worker-crawl python -m app.covers
"""

import hashlib
import os
import re
import tempfile
from pathlib import Path

from app.logs import get_logger

log = get_logger(__name__)

MAX_BYTES = 5 * 1024 * 1024
FILE_RE = re.compile(r"^[0-9a-f]{40}\.(jpg|png|webp|gif)$")
_MAGIC = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


class CoverError(ValueError):
    pass


def image_ext(data: bytes) -> str | None:
    """File extension from the image's magic bytes; None if it is not a supported image."""
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def url_key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()


def existing_file(covers_dir: Path, url: str) -> str | None:
    """Name of the stored copy of `url`, if there is one."""
    key = url_key(url)
    for ext in ("jpg", "png", "webp", "gif"):
        if (covers_dir / f"{key}.{ext}").is_file():
            return f"{key}.{ext}"
    return None


def save(covers_dir: Path, url: str, data: bytes) -> str:
    """Validate and atomically write a downloaded cover. Returns the file name."""
    if len(data) > MAX_BYTES:
        raise CoverError(f"cover too large ({len(data)} bytes)")
    ext = image_ext(data)
    if ext is None:
        raise CoverError("not an image")
    name = f"{url_key(url)}.{ext}"
    covers_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=covers_dir, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, covers_dir / name)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return name


def ensure_cover(client, covers_dir: Path, url: str | None) -> str | None:
    """Return the stored file for `url`, downloading it first if needed (raises on failure)."""
    if not url:
        return None
    return existing_file(covers_dir, url) or save(covers_dir, url, client.get_bytes(url, max_bytes=MAX_BYTES))


def backfill(limit: int | None = None, client=None) -> tuple[int, int]:
    """Download covers for games that have a cover URL but no stored copy. Returns (ok, failed).

    Newest games first, as they are the ones people open. `client` is for tests; by default a
    Metacritic client sharing the global rate limiter is used.
    """
    from sqlalchemy import select, update

    from app.config import get_settings
    from app.db import session_scope
    from app.models import Game

    settings = get_settings()
    covers_dir = Path(settings.covers_dir)
    with session_scope() as s:
        stmt = select(Game.id, Game.cover_url).where(Game.cover_url.is_not(None), Game.cover_file.is_(None))
        rows = s.execute(stmt.order_by(Game.release_date.desc().nulls_last(), Game.id.desc()).limit(limit)).all()
    if not rows:
        return 0, 0
    own_client = client is None
    if own_client:
        from app.monitoring import get_redis
        from app.scraper.client import MetacriticClient, RedisRateLimiter

        client = MetacriticClient(settings, RedisRateLimiter(get_redis(), settings.request_interval_seconds))
    ok = failed = 0
    try:
        for game_id, url in rows:
            try:
                name = ensure_cover(client, covers_dir, url)
            except Exception as exc:  # noqa: BLE001 - keep going; the crawl retries later
                failed += 1
                log.warning("covers.backfill_failed", game_id=game_id, error=str(exc))
                continue
            with session_scope() as s:
                s.execute(update(Game).where(Game.id == game_id).values(cover_file=name))
            ok += 1
            if ok % 50 == 0:
                log.info("covers.backfill_progress", done=ok, failed=failed, total=len(rows))
    finally:
        if own_client:
            client.close()
    log.info("covers.backfill_done", stored=ok, failed=failed)
    return ok, failed


if __name__ == "__main__":
    from app.logs import configure_logging

    configure_logging()
    stored, failed = backfill()
    print(f"covers stored: {stored}, failed: {failed}")
