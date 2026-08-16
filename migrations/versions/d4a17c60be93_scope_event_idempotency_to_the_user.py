"""scope study_events idempotency keys to the user

The Idempotency-Key is chosen by a client that knows nothing about other
accounts, so any natural choice - "today's date and how far I had got" - is one
a second learner will pick too. Under the old global UNIQUE, the second
person's study was answered with "already applied" and silently dropped.

The original constraint is unnamed, so it cannot be dropped by name on SQLite.
`copy_from` rebuilds the table from an explicit definition that simply omits
it, which is the portable way to remove an anonymous constraint.

Revision ID: d4a17c60be93
Revises: c7e3b81a45d2
Create Date: 2026-08-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd4a17c60be93'
down_revision: Union[str, Sequence[str], None] = 'c7e3b81a45d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table(*constraints) -> sa.Table:
    """study_events as it stands, minus whichever unique constraint the caller
    is replacing. Batch mode recreates the table from this."""
    return sa.Table(
        'study_events',
        sa.MetaData(),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('plan_id', sa.Uuid(), nullable=False),
        sa.Column('credited_local_date', sa.Date(), nullable=False),
        sa.Column('units', sa.SmallInteger(), nullable=False),
        sa.Column('from_ordinal', sa.Integer(), nullable=False),
        sa.Column('to_ordinal', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('idempotency_key', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['study_plans.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        *constraints,
    )


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('study_events', copy_from=_table()) as batch_op:
        batch_op.create_unique_constraint(
            'uq_event_idempotency', ['user_id', 'idempotency_key']
        )


def downgrade() -> None:
    """Downgrade schema.

    Restoring the global constraint can fail if two learners legitimately share
    a key under the new rules, so the colliding guards are cleared first. They
    are replay markers, not study records - the events themselves stay.
    """
    op.execute(
        "UPDATE study_events SET idempotency_key = NULL WHERE idempotency_key IN ("
        "  SELECT idempotency_key FROM study_events WHERE idempotency_key IS NOT NULL"
        "  GROUP BY idempotency_key HAVING COUNT(*) > 1)"
    )
    with op.batch_alter_table(
        'study_events',
        copy_from=_table(sa.UniqueConstraint('user_id', 'idempotency_key',
                                             name='uq_event_idempotency')),
    ) as batch_op:
        batch_op.drop_constraint('uq_event_idempotency', type_='unique')
        # Named, unlike the original: alembic cannot emit an anonymous
        # constraint, and a name is what makes it droppable next time.
        batch_op.create_unique_constraint(
            'uq_event_idempotency_key', ['idempotency_key']
        )
