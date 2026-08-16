"""record learning that happened before, or away from, this app

Its own table rather than a StudyPlan with the cursor wound forward. A plan is
a record of days this app judged; this is a declaration about the past, worth
no points and no streak. Putting invented history into the table the whole
economy trusts is how a streak stops meaning anything.

Revision ID: e8b2f4a91c07
Revises: d4a17c60be93
Create Date: 2026-08-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e8b2f4a91c07'
down_revision: Union[str, Sequence[str], None] = 'd4a17c60be93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'prior_learning',
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('tractate_id', sa.Integer(), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.CheckConstraint('ordinal >= 0', name='ck_prior_non_negative'),
        sa.ForeignKeyConstraint(['tractate_id'], ['tractates.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'tractate_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('prior_learning')
