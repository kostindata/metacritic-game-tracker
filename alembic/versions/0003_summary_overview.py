"""summary overview and prompt version

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-25 15:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('review_summaries', sa.Column('overview', sa.Text(), nullable=True))
    op.add_column('review_summaries', sa.Column('prompt_version', sa.Integer(), nullable=True))
    # Existing summaries came from the first (short, English) prompt; they get regenerated.
    op.execute("UPDATE review_summaries SET prompt_version = 1 WHERE status = 'ok'")


def downgrade() -> None:
    op.drop_column('review_summaries', 'prompt_version')
    op.drop_column('review_summaries', 'overview')
