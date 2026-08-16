"""Database schema.

Design notes that drive the shape of these tables:

1. `study_days` is a *ledger*, not a cache. Exactly one row per user per local
   calendar date, created lazily. Every gamification decision ("was this day
   missed?", "did a freeze cover it?") is a persisted fact with a status, not
   something recomputed from timestamps. That makes the history auditable and
   makes the settlement job idempotent.

2. `point_transactions` is append-only and carries a unique `idempotency_key`.
   `user_stats.total_points` is a materialised balance. The ledger is the truth;
   the balance is a denormalisation you can always rebuild with
   `SELECT SUM(amount) ... GROUP BY user_id`.

3. Streak state lives on `user_stats` and is *also* snapshotted per day
   (`study_days.streak_after`). The snapshot is what lets you render a history
   heatmap and debug "why did my streak break on the 14th".

4. All instants are stored as timezone-aware UTC. All *dates* are the user's
   local dates (a "day" is a human concept and belongs in their timezone).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


#: Portability shims. The design targets PostgreSQL, but local development and
#: the test suite run on SQLite, so the two type choices that are Postgres-only
#: get a generic fallback. `Uuid` renders as native UUID on Postgres and
#: CHAR(32) elsewhere; JSONB degrades to JSON.
UUIDType = Uuid(as_uuid=True)
JSONType = JSON().with_variant(JSONB(), "postgresql")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class StudyWeek(enum.StrEnum):
    """How many days a week the learner signed up for.

    The only calendar rule in the app. A five-day week rests on Friday and
    Shabbat - the Israeli working week - and those two days are simply not
    counted: no quota, no penalty, no effect on the streak. A seven-day week
    treats every day alike, Shabbat included.
    """

    SEVEN_DAYS = "seven_days"
    FIVE_DAYS = "five_days"


class DayKind(enum.StrEnum):
    """Whether this date carries a quota for this user."""

    WEEKDAY = "weekday"
    REST_DAY = "rest_day"  # off by the plan's week mode


class DayStatus(enum.StrEnum):
    """Terminal statuses are never recomputed - that is what makes settlement
    idempotent. Only PENDING is open."""

    PENDING = "pending"
    COMPLETED = "completed"  # goal met -> points awarded, streak += 1
    MISSED = "missed"  # goal not met -> penalty, streak -> 0
    FROZEN_ITEM = "frozen_item"  # missed, but a Streak Freeze absorbed it
    REST_DAY = "rest_day"  # a day off the plan asks nothing of
    EXEMPT = "exempt"  # before signup, paused plan, vacation


#: Statuses that hold the streak steady without advancing it. A day that is
#: neither credited nor punished must not silently break the chain.
NEUTRAL_STATUSES = frozenset(
    {DayStatus.FROZEN_ITEM, DayStatus.REST_DAY, DayStatus.EXEMPT}
)
TERMINAL_STATUSES = frozenset(
    {DayStatus.COMPLETED, DayStatus.MISSED} | NEUTRAL_STATUSES
)


class CreditSource(enum.StrEnum):
    APP = "app"  # logged in-app that day
    BACKFILL = "backfill"  # support tooling / imports


class TxnType(enum.StrEnum):
    DAILY_STUDY = "daily_study"
    MISS_PENALTY = "miss_penalty"
    PURCHASE = "purchase"
    MILESTONE = "milestone"
    ADMIN_ADJUST = "admin_adjust"


class PlanStatus(enum.StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()

    # Google's `sub` is the stable identifier for Google accounts. Email is
    # mutable and reusable across Google Workspace accounts, so it must never
    # be the join key *there*. Null for accounts that sign in with a password.
    google_sub: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True
    )
    email: Mapped[str] = mapped_column(String(320), index=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    #: scrypt digest, format `scrypt$n$r$p$salt$hash`. Null for Google
    #: accounts - the two sign-in methods are alternatives, and an account with
    #: neither cannot be logged into at all.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(Text)

    # Timezone is required for every gamification decision - it defines when
    # "a day" ends. IANA name, e.g. "Asia/Jerusalem".
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Jerusalem")

    #: Five days a week (Sunday-Thursday) or all seven. Lives on the user and
    #: not the plan: it describes the person's rhythm, and switching tractate
    #: should not quietly put them back to seven days a week.
    study_week: Mapped[StudyWeek] = mapped_column(
        String(16), default=StudyWeek.SEVEN_DAYS
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    stats: Mapped[UserStats] = relationship(back_populates="user", uselist=False)
    plans: Mapped[list[StudyPlan]] = relationship(back_populates="user")

    __table_args__ = (
        # For a password account the email *is* the identity, so it has to be
        # unique - but only among password accounts. Constraining it globally
        # would import the Workspace email-reuse problem into the Google path,
        # which is exactly what `google_sub` exists to avoid.
        Index(
            "uq_local_email",
            "email",
            unique=True,
            postgresql_where=password_hash.is_not(None),
            sqlite_where=password_hash.is_not(None),
        ),
    )


class UserStats(Base):
    """Hot-path counters. One row per user, and the row we take a `FOR UPDATE`
    lock on to serialise all scoring writes for that user."""

    __tablename__ = "user_stats"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    total_points: Mapped[int] = mapped_column(Integer, default=0)
    lifetime_points_earned: Mapped[int] = mapped_column(Integer, default=0)

    current_streak: Mapped[int] = mapped_column(Integer, default=0)
    longest_streak: Mapped[int] = mapped_column(Integer, default=0)

    #: Last local date that has been *finalised*. Settlement resumes from
    #: last_settled_date + 1. Never advances past a day still awaiting a
    #: Shabbat report.
    last_settled_date: Mapped[date | None] = mapped_column(Date)

    total_units_completed: Mapped[int] = mapped_column(Integer, default=0)
    freezes_consumed: Mapped[int] = mapped_column(Integer, default=0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="stats")

    __table_args__ = (
        CheckConstraint("total_points >= 0", name="ck_points_non_negative"),
        CheckConstraint("current_streak >= 0", name="ck_streak_non_negative"),
    )


class RefreshToken(Base):
    """Rotating refresh tokens. Only the hash is stored - a database leak must
    not hand out live sessions."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )
    user_agent: Mapped[str | None] = mapped_column(String(400))


# --------------------------------------------------------------------------- #
# Reference data: the text itself
# --------------------------------------------------------------------------- #


class Tractate(Base):
    """Seeded reference data - 63 masechtot."""

    __tablename__ = "tractates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)  # "berakhot"
    name_he: Mapped[str] = mapped_column(String(64))  # "ברכות"
    name_en: Mapped[str] = mapped_column(String(64))
    #: The book title Sefaria actually resolves, e.g. "Mishnah Berakhot".
    #: Every text and commentary ref is built from this, so it must be their
    #: spelling and not ours (Uktzin vs Oktzin).
    sefaria_title: Mapped[str] = mapped_column(String(96), default="")
    seder: Mapped[str] = mapped_column(String(32))  # "zeraim"
    order_index: Mapped[int] = mapped_column(SmallInteger)
    chapter_count: Mapped[int] = mapped_column(SmallInteger)
    #: Denormalised COUNT(mishnayot). Drives the completion estimate without a
    #: join, and is the one number the onboarding screen needs.
    mishnayot_count: Mapped[int] = mapped_column(Integer)


#: The text itself is not in the database at all. It ships as gzipped JSON
#: under `app/data/texts/` and is read straight off disk by
#: `app/services/texts.py` - see the note there for why.


class Mishnah(Base):
    """One addressable unit of study.

    `ordinal` is the trick that keeps progress arithmetic trivial: a 1-based
    running index within the tractate, so "user is at mishnah 47" is a single
    integer compare instead of (chapter, number) tuple logic.
    """

    __tablename__ = "mishnayot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tractate_id: Mapped[int] = mapped_column(
        ForeignKey("tractates.id", ondelete="CASCADE"), index=True
    )
    chapter: Mapped[int] = mapped_column(SmallInteger)
    number: Mapped[int] = mapped_column(SmallInteger)
    ordinal: Mapped[int] = mapped_column(Integer)
    sefaria_ref: Mapped[str | None] = mapped_column(String(128))  # "Berakhot 1:1"

    __table_args__ = (
        UniqueConstraint("tractate_id", "ordinal", name="uq_mishnah_ordinal"),
        UniqueConstraint("tractate_id", "chapter", "number", name="uq_mishnah_addr"),
    )


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #


class StudyPlan(Base):
    __tablename__ = "study_plans"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    tractate_id: Mapped[int] = mapped_column(ForeignKey("tractates.id"))

    daily_goal: Mapped[int] = mapped_column(SmallInteger, default=2)
    start_date: Mapped[date] = mapped_column(Date)

    #: Recomputed whenever daily_goal changes or progress drifts. Purely
    #: informational - never used for scoring.
    estimated_end_date: Mapped[date | None] = mapped_column(Date)

    #: How many mishnayot are done. Equals the `ordinal` of the last completed
    #: mishnah, so the next one to learn is `current_ordinal + 1`.
    current_ordinal: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[PlanStatus] = mapped_column(
        String(16), default=PlanStatus.ACTIVE, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="plans")

    __table_args__ = (
        CheckConstraint("daily_goal BETWEEN 1 AND 50", name="ck_goal_range"),
        # One active plan per user. Postgres partial unique index.
        Index(
            "uq_one_active_plan",
            "user_id",
            unique=True,
            postgresql_where=(status == PlanStatus.ACTIVE),
            sqlite_where=(status == PlanStatus.ACTIVE),
        ),
    )


# --------------------------------------------------------------------------- #
# The day ledger
# --------------------------------------------------------------------------- #


class StudyDay(Base):
    __tablename__ = "study_days"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("study_plans.id", ondelete="CASCADE")
    )

    #: The user's LOCAL date. Never a UTC date.
    local_date: Mapped[date] = mapped_column(Date)
    day_kind: Mapped[DayKind] = mapped_column(String(16), default=DayKind.WEEKDAY)

    #: The plan's daily goal, or 0 on a rest day. Stored per day rather than
    #: read from the plan so that changing the goal cannot re-score a day that
    #: was already judged against the old one.
    required_units: Mapped[int] = mapped_column(SmallInteger, default=0)
    completed_units: Mapped[int] = mapped_column(SmallInteger, default=0)

    status: Mapped[DayStatus] = mapped_column(String(24), default=DayStatus.PENDING)
    credit_source: Mapped[CreditSource | None] = mapped_column(String(16))

    points_awarded: Mapped[int] = mapped_column(Integer, default=0)
    multiplier: Mapped[float] = mapped_column(Numeric(4, 2), default=1.0)
    #: Streak value *after* this day was resolved. Snapshot for history/debug.
    streak_after: Mapped[int | None] = mapped_column(Integer)

    first_logged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # The backbone of idempotency: a day cannot be resolved twice.
        UniqueConstraint("user_id", "local_date", name="uq_user_day"),
        Index("ix_days_user_date_desc", "user_id", local_date.desc()),
        # Cheap lookup for the nightly worker.
        Index(
            "ix_days_open",
            "user_id",
            "local_date",
            postgresql_where=status == DayStatus.PENDING,
            sqlite_where=status == DayStatus.PENDING,
        ),
        CheckConstraint("completed_units >= 0", name="ck_units_non_negative"),
    )


class StudyEvent(Base):
    """Append-only record of the user physically logging study. Distinct from
    `study_days`: a day is a *judgement*, an event is an *action*. Two logs of
    1 mishnah each produce two events and one day row."""

    __tablename__ = "study_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("study_plans.id"))
    credited_local_date: Mapped[date] = mapped_column(Date)

    units: Mapped[int] = mapped_column(SmallInteger)
    from_ordinal: Mapped[int] = mapped_column(Integer)
    to_ordinal: Mapped[int] = mapped_column(Integer)
    source: Mapped[CreditSource] = mapped_column(String(16), default=CreditSource.APP)

    #: Client-supplied Idempotency-Key header. Stops a retried POST from
    #: advancing the reading cursor twice.
    #:
    #: Unique **per user**, never globally. The key is chosen by a client that
    #: knows nothing about other accounts, so any natural choice ("today's date
    #: and how far I had got") collides constantly - and a global constraint
    #: turns one learner's key into a silent no-op for everyone else who picks
    #: the same one.
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_event_idempotency"),
    )


# --------------------------------------------------------------------------- #
# Points ledger + shop
# --------------------------------------------------------------------------- #


class PointTransaction(Base):
    __tablename__ = "point_transactions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    amount: Mapped[int] = mapped_column(Integer)  # signed
    txn_type: Mapped[TxnType] = mapped_column(String(24))
    related_date: Mapped[date | None] = mapped_column(Date)

    #: Deterministic, e.g. "daily:<user>:2026-08-13". A UNIQUE index turns
    #: "award points for that day" into a safely repeatable operation, which is
    #: what lets the settlement job run from a request handler AND a cron.
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)

    balance_after: Mapped[int | None] = mapped_column(Integer)
    meta: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_txn_user_created", "user_id", created_at.desc()),
    )


class ShopItem(Base):
    """Prices live in the database so a balance change is a config edit, not a
    deploy."""

    __tablename__ = "shop_items"

    sku: Mapped[str] = mapped_column(String(48), primary_key=True)  # "streak_freeze"
    name_he: Mapped[str] = mapped_column(String(120))
    description_he: Mapped[str | None] = mapped_column(Text)
    cost_points: Mapped[int] = mapped_column(Integer)
    max_owned: Mapped[int] = mapped_column(SmallInteger, default=3)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class UserInventory(Base):
    __tablename__ = "user_inventory"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    sku: Mapped[str] = mapped_column(ForeignKey("shop_items.sku"), primary_key=True)
    quantity: Mapped[int] = mapped_column(SmallInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_inventory_non_negative"),
    )


class FreezeUsage(Base):
    """Which freeze covered which day. Needed so the UI can say "your freeze
    saved you on Tuesday" and so support can audit a disputed streak."""

    __tablename__ = "freeze_usages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    covered_date: Mapped[date] = mapped_column(Date)
    used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "covered_date", name="uq_freeze_per_day"),
    )


# --------------------------------------------------------------------------- #
# Achievements (extension point - schema now, rules later)
# --------------------------------------------------------------------------- #


class Achievement(Base):
    __tablename__ = "achievements"

    code: Mapped[str] = mapped_column(String(48), primary_key=True)
    name_he: Mapped[str] = mapped_column(String(120))
    description_he: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(String(64))
    points_reward: Mapped[int] = mapped_column(Integer, default=0)


class UserAchievement(Base):
    __tablename__ = "user_achievements"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    code: Mapped[str] = mapped_column(ForeignKey("achievements.code"), primary_key=True)
    earned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
