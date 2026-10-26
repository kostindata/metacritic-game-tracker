from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://mc:mc@localhost:5432/mc"
    redis_url: str = "redis://localhost:6379/0"

    # LLM
    openrouter_api_key: str = ""
    llm_model: str = "deepseek/deepseek-v4.1-flash"
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_timeout_seconds: float = 120.0
    llm_max_attempts: int = 3
    summary_language: str = "Russian"
    # Includes the model's hidden reasoning tokens, so keep it generous.
    llm_max_tokens: int = 8000
    # Hidden "thinking" before answering. Off: ~3x faster (median ≈7 s instead of ≈19 s per
    # summary), with slightly shorter points.
    llm_reasoning: bool = False

    # Crawl behaviour
    games_per_run: int = 500
    # Games fetched in parallel within a run. All of them share the global rate limit
    # (request_interval_seconds), so this hides network latency rather than adding load.
    crawl_concurrency: int = 4
    # First run of the day takes this many games from the New Releases section (also the carousel).
    new_releases_count: int = 20
    max_reviews_per_type: int = 30
    min_reviews_for_summary: int = 3
    # Safety cap on listing pages walked in one run (500 games need ~21 pages of 24).
    max_listing_pages_per_run: int = 100
    crawl_schedule_minute: int = 0  # hourly at HH:MM
    # All schedules, the crawl day and displayed times use this zone (Moscow = UTC+3, no DST).
    timezone: str = "Europe/Moscow"
    daily_reset_hour: int = 0  # local hour at which "today" starts
    # Refreshed before every Metacritic request, so a dead worker frees the lock quickly.
    lock_ttl_seconds: int = 120

    # HTTP politeness
    request_interval_seconds: float = 1.0
    http_timeout_seconds: float = 30.0
    http_max_attempts: int = 4
    http_backoff_base_seconds: float = 2.0
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
    playwright_enabled: bool = True

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # json | console

    # Identification of this process in the dashboard
    worker_name: str = "worker"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
