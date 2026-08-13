"""The points shop - currently just the Streak Freeze."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.models import ShopItem, TxnType, User, UserInventory
from app.services import ledger
from app.services.study import StudyError

STREAK_FREEZE_SKU = "streak_freeze"


@dataclass(slots=True)
class PurchaseResult:
    sku: str
    quantity_owned: int
    points_spent: int
    balance: int


def purchase(
    session: Session,
    user: User,
    sku: str,
    *,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> PurchaseResult:
    now = now or clock.now()

    item = session.get(ShopItem, sku)
    if item is None or not item.is_active:
        raise StudyError("unknown_item", f"no item {sku!r}", 404)

    stats = ledger.lock_stats(session, user.id)

    inventory = session.execute(
        ledger.maybe_for_update(
            session,
            select(UserInventory).where(
                UserInventory.user_id == user.id, UserInventory.sku == sku
            ),
        )
    ).scalar_one_or_none()
    if inventory is None:
        inventory = UserInventory(user_id=user.id, sku=sku, quantity=0)
        session.add(inventory)
        session.flush()

    if inventory.quantity >= item.max_owned:
        raise StudyError(
            "inventory_full",
            f"you can hold at most {item.max_owned} of this item",
            409,
        )
    if stats.total_points < item.cost_points:
        raise StudyError(
            "insufficient_points",
            f"needs {item.cost_points}, balance {stats.total_points}",
            402,
        )

    key = ledger.purchase_key(user.id, sku, idempotency_key or uuid.uuid4().hex)
    posted = ledger.post_transaction(
        session,
        stats,
        amount=-item.cost_points,
        txn_type=TxnType.PURCHASE,
        idempotency_key=key,
        meta={"sku": sku},
    )
    if posted is None:  # replayed request - inventory already granted
        return PurchaseResult(sku, inventory.quantity, 0, stats.total_points)

    inventory.quantity += 1
    session.flush()
    return PurchaseResult(
        sku=sku,
        quantity_owned=inventory.quantity,
        points_spent=item.cost_points,
        balance=stats.total_points,
    )


def seed_default_items(session: Session, cost: int) -> None:
    """Called from a migration or a bootstrap command."""
    if session.get(ShopItem, STREAK_FREEZE_SKU) is None:
        session.add(
            ShopItem(
                sku=STREAK_FREEZE_SKU,
                name_he="הקפאת רצף",
                description_he=(
                    "מגינה על הרצף שלך ליום אחד שהוחמץ - בלי איבוד נקודות."
                ),
                cost_points=cost,
                max_owned=3,
            )
        )
