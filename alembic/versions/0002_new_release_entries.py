"""new release entries for the carousel

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25 14:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'new_release_entries',
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('slug', sa.String(length=255), nullable=False),
        sa.Column('title', sa.String(length=500), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('day', 'position'),
    )
    op.create_index(op.f('ix_new_release_entries_slug'), 'new_release_entries', ['slug'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_new_release_entries_slug'), table_name='new_release_entries')
    op.drop_table('new_release_entries')
