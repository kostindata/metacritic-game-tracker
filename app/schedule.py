"""Schedule definitions shared by Celery Beat and the dashboard."""

from datetime import datetime, timedelta, timezone, tzinfo

from celery.schedules import crontab

from app.config import Settings


def beat_schedule(settings: Settings) -> dict:
    return {
        "hourly-crawl": {
            "task": "app.tasks.scheduled_crawl",
            "schedule": crontab(minute=settings.crawl_schedule_minute),
            # A tick that cannot start promptly is dropped instead of firing late.
            "options": {"queue": "crawl", "expires": 15 * 60},
        },
        "cleanup-runs": {
            "task": "app.tasks.cleanup_runs",
            "schedule": 60.0,
            "options": {"queue": "crawl", "expires": 50},
        },
        "daily-reset": {
            "task": "app.tasks.daily_reset",
            "schedule": crontab(minute=0, hour=settings.daily_reset_hour),
            "options": {"queue": "crawl", "expires": 60 * 60},
        },
    }


def next_crawl_time(now: datetime, minute: int, tz: tzinfo = timezone.utc) -> datetime:
    """Next HH:`minute` local time strictly after `now` (matches crontab(minute=minute))."""
    now = now.astimezone(tz)
    candidate = now.replace(minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(hours=1)
    return candidate


def next_reset_time(now: datetime, hour: int, tz: tzinfo = timezone.utc) -> datetime:
    """Next `hour`:00 local time strictly after `now`."""
    now = now.astimezone(tz)
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate
