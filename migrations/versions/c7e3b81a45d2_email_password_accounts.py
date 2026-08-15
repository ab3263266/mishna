"""email + password accounts

Adds a second way in, alongside Google. Two schema consequences:

* `google_sub` becomes nullable — a password account has no Google identity.
  It stays unique, which on both dialects permits many NULLs.
* Email becomes unique **among password accounts only**, via a partial index.
  For those accounts the address is the identity; constraining it globally
  would import the Workspace email-reuse problem into the Google path, which
  is the whole reason `google_sub` is the join key there.

Revision ID: c7e3b81a45d2
Revises: a1f4c92d7e30
Create Date: 2026-08-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c7e3b81a45d2'
down_revision: Union[str, Sequence[str], None] = 'a1f4c92d7e30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('password_hash', sa.String(length=255),
                                      nullable=True))
        batch_op.alter_column('google_sub', existing_type=sa.String(length=255),
                              nullable=True)
        batch_op.create_index(
            'uq_local_email', ['email'], unique=True,
            postgresql_where=sa.text('password_hash IS NOT NULL'),
            sqlite_where=sa.text('password_hash IS NOT NULL'),
        )


def downgrade() -> None:
    """Downgrade schema.

    Password accounts have no `google_sub`, so restoring the NOT NULL would
    fail on any row created through the new flow. They are deleted rather than
    left to break the constraint — there is no Google identity to fall back to,
    so the account genuinely cannot exist under the old schema.
    """
    op.execute("DELETE FROM users WHERE google_sub IS NULL")
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(
            'uq_local_email',
            postgresql_where=sa.text('password_hash IS NOT NULL'),
            sqlite_where=sa.text('password_hash IS NOT NULL'),
        )
        batch_op.alter_column('google_sub', existing_type=sa.String(length=255),
                              nullable=False)
        batch_op.drop_column('password_hash')
