"""Start AI summary generation when a game is opened in the UI."""

from sqlalchemy import update

from app.config import get_settings
from app.db import session_scope
from app.llm.summarizer import claim_for_generation
from app.logs import get_logger
from app.models import ReviewSummary
from app.monitoring import notify

log = get_logger(__name__)


def request_summaries(game_id: int) -> list[str]:
    """Queue LLM runs for the game's summaries that need one. Returns the queued kinds.

    Cheap when nothing is needed (one UPDATE that matches no rows), so it is safe to call on
    every page view.
    """
    if not get_settings().openrouter_api_key:
        return []
    from app.tasks import summarize_game  # local import: keeps web startup light

    with session_scope() as s:
        kinds = claim_for_generation(s, game_id)
    for kind in kinds:
        try:
            summarize_game.apply_async(args=[game_id, kind])
        except Exception as exc:  # noqa: BLE001 - broker down: show the error instead of spinning
            with session_scope() as s:
                s.execute(
                    update(ReviewSummary)
                    .where(ReviewSummary.game_id == game_id, ReviewSummary.kind == kind)
                    .values(status="error", message=f"could not queue generation: {exc}"[:1000])
                )
    if kinds:
        log.info("llm.summaries_requested", game_id=game_id, kinds=kinds)
        notify("llm", game_id=game_id)
    return kinds
