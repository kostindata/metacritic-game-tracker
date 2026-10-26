from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.llm.summarizer import claim_for_generation


class CapturingSession:
    def __init__(self):
        self.statements = []

    def scalars(self, stmt):
        self.statements.append(stmt)
        return ["critic"]


def test_claim_is_one_atomic_update_returning_claimed_kinds():
    session = CapturingSession()
    assert claim_for_generation(session, 42) == ["critic"]
    [stmt] = session.statements
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert sql.startswith("UPDATE review_summaries SET status='pending'")
    assert "RETURNING review_summaries.kind" in sql
    assert "review_summaries.game_id = 42" in sql
    for condition in ("'outdated'", "'error'", "'pending'"):
        assert condition in sql
    assert "not_enough" not in sql  # never sends too-few-reviews games to the LLM
    assert "'ok'" not in sql and "prompt_version" not in sql  # a ready summary is always reused


def _render(**summaries):
    from app.web.main import templates

    game = SimpleNamespace(slug="some-game", url="https://www.metacritic.com/game/some-game/")
    return templates.get_template("_summaries.html").render(
        game=game,
        summaries=summaries,
        generating=any(s.status == "pending" for s in summaries.values()),
        ai_enabled=True,
    )


def _summary(status, likes=(), **kw):
    defaults = dict(overview="Обзор консенсуса.", dislikes=["минус"], review_count=12, message=None,
                    summary_generated_at=datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc))
    return SimpleNamespace(status=status, likes=list(likes), **{**defaults, **kw})


def test_ready_summary_is_labelled_and_does_not_poll():
    html = _render(critic=_summary("ok", ["плюс"]))
    assert "Сгенерировано ИИ" in html
    assert "На основе 12 отзывов" in html
    # no model name and no disclaimer
    assert "deepseek" not in html and "неточност" not in html
    assert "hx-trigger" not in html


def test_pending_summary_shows_progress_and_polls():
    html = _render(user=_summary("pending"))
    assert 'hx-get="/games/some-game/summaries"' in html and 'hx-trigger="every 2s"' in html
    assert "ИИ анализирует отзывы и готовит сводку" in html
    assert "Сгенерировано ИИ" in html


def test_not_enough_reviews_has_no_ai_label():
    html = _render(critic=_summary("not_enough"))
    assert "Пока недостаточно отзывов" in html
    assert "Сгенерировано ИИ" not in html



def test_game_card_links_to_game_without_similar_list():
    from app.web.main import templates

    g = SimpleNamespace(id=7, slug="abyss-ring", title="Abyss Ring", cover_url=None, release_date=None,
                        platforms=[SimpleNamespace(platform="PC")], best_metascore=None, best_user_score=10.0)
    html = templates.get_template("_game_card.html").render(g=g)
    assert '<a class="tile-link" href="/games/abyss-ring">Abyss Ring</a>' in html
    assert "Похожие" not in html
    assert ">10</span>" in html and "10.0" not in html  # a perfect user score fits the small circle
