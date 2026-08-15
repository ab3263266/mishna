"""Development-only endpoints.

Mounted only when `dev_mode` is on. Two of these would be serious
vulnerabilities in production - `login` mints a session for any email with no
credential, and `time` moves the clock for the whole process - so the flag is
checked at mount time in main.py *and* again per request here.

Time travel exists because the features worth demonstrating (a 4-day
multiplier, a rest day, a missed-day penalty) are otherwise only observable by
waiting a week.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.api.deps import CurrentUser, DbSession
from app.core import clock
from app.api.auth_google import set_refresh_cookie
from app.core.config import get_settings
from app.core.security import GoogleIdentity, issue_tokens, upsert_user
from app.models import (
    FreezeUsage,
    PointTransaction,
    StudyDay,
    StudyEvent,
    StudyPlan,
    User,
    UserInventory,
    UserStats,
)

router = APIRouter(prefix="/dev", tags=["dev"])


def _guard() -> None:
    if not get_settings().dev_mode:
        raise HTTPException(404, {"code": "not_found"})


class DevLoginIn(BaseModel):
    email: str = "demo@example.com"
    display_name: str = "לומד/ת הדגמה"
    timezone: str = "Asia/Jerusalem"


@router.post("/login")
def dev_login(payload: DevLoginIn, session: DbSession, response: Response) -> dict:
    """Passwordless login, standing in for the Google flow."""
    _guard()

    identity = GoogleIdentity(
        sub=f"dev|{payload.email}",
        email=payload.email,
        email_verified=True,
        name=payload.display_name,
        picture=None,
    )
    existed = session.execute(
        select(User.id).where(User.google_sub == identity.sub)
    ).scalar_one_or_none()

    # Deliberately does not touch `study_week`: it is chosen once at onboarding
    # and changed from settings, so writing it on every login would silently
    # reset a returning learner's track.
    user = upsert_user(session, identity, payload.timezone)

    if existed is None:
        session.add(UserStats(user_id=user.id))
    session.flush()

    pair = issue_tokens(session, user)
    # Set the same HttpOnly cookie the Google flow uses, so the dev path
    # exercises the real session mechanism instead of a parallel one.
    set_refresh_cookie(response, pair.refresh_token)
    return {
        "access_token": pair.access_token,
        "refresh_token": pair.refresh_token,
        "expires_in": pair.expires_in,
        "is_new_user": existed is None,
        "user": {"email": user.email, "name": user.display_name},
    }


class TimeIn(BaseModel):
    days: int = Field(default=0, ge=-400, le=400)
    hours: int = Field(default=0, ge=-72, le=72)
    reset: bool = False


@router.post("/time")
def dev_time(payload: TimeIn) -> dict:
    _guard()
    if payload.reset:
        clock.reset()
    else:
        clock.shift(timedelta(days=payload.days, hours=payload.hours))
    return dev_time_state()


@router.get("/time")
def dev_time_state() -> dict:
    _guard()
    simulated = clock.now()
    return {
        "simulated_utc": simulated,
        "offset_hours": round(clock.offset().total_seconds() / 3600, 2),
        "weekday": simulated.strftime("%A"),
    }


@router.post("/reset")
def dev_reset(user: CurrentUser, session: DbSession) -> dict:
    """Wipe this user's progress but keep the account and the reference data."""
    _guard()

    for model in (
        StudyEvent,
        StudyDay,
        PointTransaction,
        FreezeUsage,
        UserInventory,
        StudyPlan,
    ):
        session.execute(delete(model).where(model.user_id == user.id))

    stats = session.get(UserStats, user.id)
    stats.total_points = 0
    stats.lifetime_points_earned = 0
    stats.current_streak = 0
    stats.longest_streak = 0
    stats.last_settled_date = None
    stats.total_units_completed = 0
    stats.freezes_consumed = 0

    clock.reset()
    return {"ok": True}


class GrantIn(BaseModel):
    points: int = Field(default=200, ge=0, le=10_000)


@router.post("/grant")
def dev_grant(payload: GrantIn, user: CurrentUser, session: DbSession) -> dict:
    """Top up points so the shop is reachable without a two-week streak."""
    _guard()
    stats = session.get(UserStats, user.id)
    stats.total_points += payload.points
    stats.lifetime_points_earned += payload.points
    return {"total_points": stats.total_points}
