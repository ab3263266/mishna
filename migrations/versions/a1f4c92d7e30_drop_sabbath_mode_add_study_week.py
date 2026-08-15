"""drop sabbath mode, add the five/seven-day study week

Friday and Shabbat stop being special calendar days. Everything that existed to
make them special - zmanim-based rest windows, the Friday double portion, the
Motzash report and its bonus - is gone. In its place the user picks a week:
seven days, or five (Sunday to Thursday).

Existing data is mapped rather than dropped:

* `observes_shabbat` becomes the week mode. Someone who kept Shabbat was
  already not learning on it, so they land on five days; everyone else on
  seven. This is the only reading that does not change what the app asks of
  them tomorrow.
* Fridays keep their study-day status (they carried a doubled quota, but they
  were a day the learner turned up for). Shabbatot and Yamim Tovim become rest
  days.
* Open `shabbat_pending` days become terminal rest days instead of falling back
  into the pending pool - re-judging a Shabbat from two months ago under the
  new rules would hand out penalties for days the learner was told were free.

Revision ID: a1f4c92d7e30
Revises: 0bcb783ad057
Create Date: 2026-08-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1f4c92d7e30'
down_revision: Union[str, Sequence[str], None] = '0bcb783ad057'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'users',
        sa.Column('study_week', sa.String(length=16), nullable=False,
                  server_default='seven_days'),
    )
    op.execute(
        "UPDATE users SET study_week = 'five_days' WHERE observes_shabbat"
    )

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('observes_shabbat')
        batch_op.drop_column('observes_yom_tov')
        batch_op.drop_column('candle_lighting_offset')
        batch_op.drop_column('in_israel')
        batch_op.drop_column('elevation_m')
        batch_op.drop_column('longitude')
        batch_op.drop_column('latitude')

    # Day kinds and statuses collapse to the two that survive.
    op.execute(
        "UPDATE study_days SET day_kind = 'weekday' WHERE day_kind = 'erev_shabbat'"
    )
    op.execute(
        "UPDATE study_days SET day_kind = 'rest_day' "
        "WHERE day_kind IN ('shabbat', 'yom_tov', 'erev_yom_tov')"
    )
    op.execute(
        "UPDATE study_days SET status = 'rest_day' "
        "WHERE status IN ('shabbat_pending', 'shabbat_unreported')"
    )

    # The ledger keeps its history; only the labels that no longer exist move.
    op.execute(
        "UPDATE point_transactions SET txn_type = 'daily_study' "
        "WHERE txn_type = 'shabbat_study'"
    )
    op.execute(
        "UPDATE point_transactions SET txn_type = 'milestone' "
        "WHERE txn_type = 'motash_bonus'"
    )
    op.execute(
        "UPDATE study_events SET source = 'backfill' WHERE source = 'shabbat_report'"
    )
    op.execute(
        "UPDATE study_days SET credit_source = 'backfill' "
        "WHERE credit_source = 'shabbat_report'"
    )

    with op.batch_alter_table('study_days', schema=None) as batch_op:
        batch_op.drop_index(
            'ix_days_open',
            postgresql_where=sa.text("status IN ('pending', 'shabbat_pending')"),
            sqlite_where=sa.text("status IN ('pending', 'shabbat_pending')"),
        )
        batch_op.create_index(
            'ix_days_open', ['user_id', 'local_date'], unique=False,
            postgresql_where=sa.text("status = 'pending'"),
            sqlite_where=sa.text("status = 'pending'"),
        )

    op.drop_table('shabbat_reports')
    # The texts now ship as files under app/data/texts/ and are read from disk.
    op.drop_table('text_cache')


def downgrade() -> None:
    """Downgrade schema.

    Structural only. The Motzash reports and the cached passages are gone; this
    brings back the columns and tables so the older code can boot, not the rows
    it used to read.
    """
    op.create_table(
        'text_cache',
        sa.Column('ref', sa.String(length=160), nullable=False),
        sa.Column('kind', sa.String(length=48), nullable=False),
        sa.Column('he_ref', sa.String(length=160), nullable=True),
        sa.Column('language', sa.String(length=8), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('license', sa.String(length=64), nullable=True),
        sa.Column('version_title', sa.String(length=200), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('ref', 'kind'),
    )
    op.create_table(
        'shabbat_reports',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('shabbat_date', sa.Date(), nullable=False),
        sa.Column('friday_date', sa.Date(), nullable=False),
        sa.Column('completed', sa.Boolean(), nullable=False),
        sa.Column('reported_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('earned_motash_bonus', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'shabbat_date', name='uq_shabbat_report'),
    )

    with op.batch_alter_table('study_days', schema=None) as batch_op:
        batch_op.drop_index(
            'ix_days_open',
            postgresql_where=sa.text("status = 'pending'"),
            sqlite_where=sa.text("status = 'pending'"),
        )
        batch_op.create_index(
            'ix_days_open', ['user_id', 'local_date'], unique=False,
            postgresql_where=sa.text("status IN ('pending', 'shabbat_pending')"),
            sqlite_where=sa.text("status IN ('pending', 'shabbat_pending')"),
        )

    op.execute(
        "UPDATE study_days SET day_kind = 'shabbat' WHERE day_kind = 'rest_day'"
    )
    op.execute(
        "UPDATE study_days SET status = 'shabbat_unreported' WHERE status = 'rest_day'"
    )

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('latitude', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('longitude', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('elevation_m', sa.Float(), nullable=False,
                                      server_default='0'))
        batch_op.add_column(sa.Column('in_israel', sa.Boolean(), nullable=False,
                                      server_default=sa.true()))
        batch_op.add_column(sa.Column('candle_lighting_offset', sa.SmallInteger(),
                                      nullable=False, server_default='18'))
        batch_op.add_column(sa.Column('observes_shabbat', sa.Boolean(),
                                      nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('observes_yom_tov', sa.Boolean(),
                                      nullable=False, server_default=sa.true()))

    op.execute(
        "UPDATE users SET observes_shabbat = false WHERE study_week = 'seven_days'"
    )
    op.drop_column('users', 'study_week')
