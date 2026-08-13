"""Pure scoring rules. No database, no clock, no I/O.

Keeping this module pure is what makes the economy testable: every rule in the
spec becomes a one-line assertion, and you can replay a user's whole history
through `points_for_completion` to verify a disputed balance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True, slots=True)
class StreakTier:
    min_streak: int
    multiplier: Decimal
    label: str


@dataclass(frozen=True, slots=True)
class ScoringRules:
    base_points: int = 10

    #: Spec 2.2: "3 consecutive days -> multiplier from the 4th day". The tiers
    #: are open-ended ranges, checked highest-first, so adding a 30-day tier is
    #: a config change rather than an `if` chain edit.
    tiers: tuple[StreakTier, ...] = field(
        default=(
            StreakTier(30, Decimal("2.5"), "מתמיד"),
            StreakTier(10, Decimal("2.0"), "אש"),
            StreakTier(4, Decimal("1.5"), "רצף"),
            StreakTier(1, Decimal("1.0"), "התחלה"),
        )
    )

    miss_penalty: int = 15
    motash_bonus: int = 5
    streak_freeze_cost: int = 120

    #: Points never go below this. A user who returns after a month should find
    #: a discouraging zero, not a debt they must dig out of before the shop
    #: works again.
    min_balance: int = 0

    def tier_for(self, streak: int) -> StreakTier:
        for tier in self.tiers:
            if streak >= tier.min_streak:
                return tier
        return StreakTier(0, Decimal("1.0"), "")

    def multiplier_for(self, streak: int) -> Decimal:
        return self.tier_for(streak).multiplier


def points_for_completion(rules: ScoringRules, streak_after: int) -> int:
    """Points for a day that was completed, given the streak it produces.

    `streak_after` is the streak *including* this day - completing your 4th
    consecutive day scores at the 4+ tier. With the defaults: days 1-3 pay 10,
    day 4 onwards pays 15.
    """
    raw = Decimal(rules.base_points) * rules.multiplier_for(streak_after)
    return int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def penalty_for_miss(rules: ScoringRules, current_balance: int) -> int:
    """Positive magnitude of the penalty, clamped so the balance cannot go
    below `min_balance`."""
    headroom = max(current_balance - rules.min_balance, 0)
    return min(rules.miss_penalty, headroom)


def next_tier_preview(rules: ScoringRules, streak: int) -> tuple[int, Decimal] | None:
    """(days_remaining, multiplier) for the next tier up - the "2 more days to
    1.5x" carrot the home screen shows. None once at the top tier."""
    higher = [t for t in rules.tiers if t.min_streak > streak]
    if not higher:
        return None
    target = min(higher, key=lambda t: t.min_streak)
    return target.min_streak - streak, target.multiplier
