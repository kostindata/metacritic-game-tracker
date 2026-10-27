"""Celery application and worker-status signal wiring."""

from celery import Celery
from celery.signals import (
    beat_init,
    worker_ready,
    worker_shutting_down,
    setup_logging,
    task_failure,
    task_postrun,
    task_prerun,
    worker_process_init,
    worker_process_shutdown,
)

from app.config import get_settings
from app.logs import configure_logging
from app.monitoring import WorkerStatus, clear_stop, current_worker, request_stop, set_current_worker
from app.schedule import beat_schedule

settings = get_settings()

celery_app = Celery("metacritic", broker=settings.redis_url, backend=settings.redis_url, include=["app.tasks"])
celery_app.conf.update(
    # Crontab entries are interpreted in the app's timezone (UTC+3 by default).
    timezone=settings.timezone,
    enable_utc=True,
    task_serializer="json",
    accept_content=["json"],
    result_expires=3600,
    worker_prefetch_multiplier=1,
    task_routes={
        "app.tasks.run_crawl": {"queue": "crawl"},
        "app.tasks.scheduled_crawl": {"queue": "crawl"},
        "app.tasks.daily_reset": {"queue": "crawl"},
        "app.tasks.cleanup_runs": {"queue": "crawl"},
        "app.tasks.refresh_similar_games": {"queue": "crawl"},
        "app.tasks.backfill_covers": {"queue": "crawl"},
        "app.tasks.summarize_game": {"queue": "llm"},
    },
    beat_schedule=beat_schedule(settings),
    broker_connection_retry_on_startup=True,
)


@setup_logging.connect
def _setup_logging(**_kwargs) -> None:
    configure_logging()


TASK_NAMES = {
    "scheduled_crawl": "сбор по расписанию",
    "daily_reset": "ежедневный сброс",
    "cleanup_runs": "проверка зависших запусков",
    "refresh_similar_games": "пересчёт похожих игр",
    "backfill_covers": "загрузка обложек",
}


def _describe(task, args, kwargs) -> str:
    name = task.name.rsplit(".", 1)[-1]
    if name == "run_crawl":
        return f"сбор данных, запуск №{(args or [None])[0]}"
    if name == "summarize_game":
        game_id, kind = (list(args or []) + [kwargs.get("game_id"), kwargs.get("kind")])[:2]
        who = "критиков" if kind == "critic" else "игроков"
        return f"ИИ-сводка отзывов {who} (игра {game_id})"
    return TASK_NAMES.get(name, name.replace("_", " "))


@worker_ready.connect
def _worker_ready(**_kwargs) -> None:
    clear_stop()


@worker_shutting_down.connect
def _worker_shutting_down(**_kwargs) -> None:
    # Lets a running crawl finish its current game, save its place and release the lock.
    request_stop()


@worker_process_init.connect
def _worker_init(**_kwargs) -> None:
    status = WorkerStatus()
    status.start_heartbeat()
    set_current_worker(status)


@beat_init.connect
def _beat_init(**_kwargs) -> None:
    status = WorkerStatus(name=f"beat@{settings.worker_name}")
    status.state.update(name="beat", detail=f"сбор каждый час в :{settings.crawl_schedule_minute:02d} · сброс дня в {settings.daily_reset_hour:02d}:00")
    status.start_heartbeat()
    set_current_worker(status)


@worker_process_shutdown.connect
def _worker_shutdown(**_kwargs) -> None:
    status = current_worker()
    if status:
        status.stop()


@task_prerun.connect
def _task_prerun(task=None, args=None, kwargs=None, **_kw) -> None:
    status = current_worker()
    if status and task:
        status.update(status="busy", task=_describe(task, args, kwargs or {}), detail=None, error=None)


@task_postrun.connect
def _task_postrun(state=None, **_kw) -> None:
    status = current_worker()
    if status and state != "FAILURE":
        status.update(status="idle", task=None, detail=None)


@task_failure.connect
def _task_failure(exception=None, **_kw) -> None:
    status = current_worker()
    if status:
        status.update(status="error", error=f"{type(exception).__name__}: {exception}"[:500])
