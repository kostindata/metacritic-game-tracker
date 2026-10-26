"""precomputed similar games

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-26 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'game_similar',
        sa.Column('game_id', sa.Integer(), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=False),
        sa.Column('similar_id', sa.Integer(), nullable=False),
        sa.Column('score', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['game_id'], ['games.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['similar_id'], ['games.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('game_id', 'rank'),
    )
    op.create_index(op.f('ix_game_similar_similar_id'), 'game_similar', ['similar_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_game_similar_similar_id'), table_name='game_similar')
    op.drop_table('game_similar')
