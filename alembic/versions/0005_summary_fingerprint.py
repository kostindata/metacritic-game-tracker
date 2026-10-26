"""fingerprint of the reviews a summary was generated from

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-26 11:00:00

"""
from types import SimpleNamespace
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.llm.fingerprint import reviews_fingerprint


revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('review_summaries', sa.Column('reviews_fingerprint', sa.String(length=64), nullable=True))

    # Existing ready summaries keep being reused: fingerprint them with the reviews stored now,
    # so only genuinely new written reviews trigger a regeneration from here on.
    bind = op.get_bind()
    summaries = bind.execute(sa.text("SELECT id, game_id, kind FROM review_summaries WHERE status = 'ok'")).all()
    for sid, game_id, kind in summaries:
        reviews = [
            SimpleNamespace(platform=r.platform, source=r.source, review_date=r.review_date, quote=r.quote)
            for r in bind.execute(
                sa.text("SELECT platform, source, review_date, quote FROM reviews WHERE game_id = :g AND kind = :k"),
                {"g": game_id, "k": kind},
            )
        ]
        bind.execute(
            sa.text("UPDATE review_summaries SET reviews_fingerprint = :f, review_count = :n WHERE id = :id"),
            {"f": reviews_fingerprint(reviews), "n": len(reviews), "id": sid},
        )


def downgrade() -> None:
    op.drop_column('review_summaries', 'reviews_fingerprint')
