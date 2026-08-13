"""Points ledger writes.

Every point that moves goes through `post_transaction`. The unique
`idempotency_key` is load-bearing: settlement runs from both a request handler
and an hourly cron, and those can race. Rather than coordinating them, we let
them both try and let Postgres discard the duplicate.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PointTransaction, TxnType, UserStats


def _upsert_insert(session: Session):
    """`INSERT ... ON CONFLICT DO NOTHING` for whichever dialect is bound.

    Both PostgreSQL and SQLite (3.24+) support the clause with the same API;
    only the import path differs.
    """
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


def lock_stats(session: Session, user_id: uuid.UUID) -> UserStats:
    """Take a row lock that serialises every scoring write for this user.

    All mutating paths (log study, settle, purchase, Shabbat report) acquire
    this first, in this order, so two concurrent requests cannot both read
    streak=3 and both write streak=4.

    SQLite has no row locks - it takes a database-wide write lock instead,
    which is strictly stronger, so skipping the clause there is safe. Do not
    read that as "locking is optional": on Postgres this is the only thing
    standing between two concurrent requests and a double-counted streak.
    """
    stmt = maybe_for_update(session, select(UserStats).where(UserStats.user_id == user_id))
    return session.execute(stmt).scalar_one()


def maybe_for_update(session: Session, stmt):
    """Add `FOR UPDATE` where the dialect has it. See `lock_stats`."""
    if session.bind.dialect.name == "postgresql":
        return stmt.with_for_update()
    return stmt


def post_transaction(
    session: Session,
    stats: UserStats,
    *,
    amount: int,
    txn_type: TxnType,
    idempotency_key: str,
    related_date: date | None = None,
    meta: dict | None = None,
) -> PointTransaction | None:
    """Append to the ledger and move the materialised balance.

    Returns None when the key already existed, meaning this exact award was
    already applied and the balance must NOT move again. Callers should treat
    None as "no-op, carry on" rather than an error.
    """
    if amount == 0:
        return None

    stmt = (
        _upsert_insert(session)(PointTransaction)
        .values(
            id=uuid.uuid4(),
            user_id=stats.user_id,
            amount=amount,
            txn_type=txn_type,
            related_date=related_date,
            idempotency_key=idempotency_key,
            balance_after=stats.total_points + amount,
            meta=meta,
        )
        .on_conflict_do_nothing(index_elements=[PointTransaction.idempotency_key])
        .returning(PointTransaction.id)
    )
    inserted_id = session.execute(stmt).scalar_one_or_none()
    if inserted_id is None:
        return None

    stats.total_points += amount
    if amount > 0:
        stats.lifetime_points_earned += amount

    return session.get(PointTransaction, inserted_id)


def rebuild_balance(session: Session, user_id: uuid.UUID) -> int:
    """Recompute the balance from the ledger. Use in a reconciliation job or
    when investigating a support ticket - the ledger, not the counter, is the
    source of truth."""
    from sqlalchemy import func

    total = session.execute(
        select(func.coalesce(func.sum(PointTransaction.amount), 0)).where(
            PointTransaction.user_id == user_id
        )
    ).scalar_one()
    return int(total)


# Deterministic key builders - one per award type, so a replay of the same day
# can never mint new points.
def daily_key(user_id: uuid.UUID, d: date) -> str:
    return f"daily:{user_id}:{d.isoformat()}"


def penalty_key(user_id: uuid.UUID, d: date) -> str:
    return f"penalty:{user_id}:{d.isoformat()}"


def motash_key(user_id: uuid.UUID, saturday: date) -> str:
    return f"motash:{user_id}:{saturday.isoformat()}"


def purchase_key(user_id: uuid.UUID, sku: str, nonce: str) -> str:
    return f"purchase:{user_id}:{sku}:{nonce}"
