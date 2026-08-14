"""HTTP surface.

Thin by design: every handler validates input, calls exactly one service, and
serialises the result. No business rule lives here - that is what makes the
rules testable without spinning up an app.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, IdempotencyKey
from app.core.security import AuthError, exchange_code, issue_tokens, rotate_refresh_token, upsert_user
from app.core import clock
from app.models import (
    Mishnah,
    PlanStatus,
    StudyEvent,
    PointTransaction,
    ShopItem,
    StudyDay,
    StudyPlan,
    Tractate,
    UserInventory,
)
from app.services import progress, settlement, shop, study, texts
from app.services.study import StudyError

router = APIRouter()


def _handle(exc: StudyError | AuthError) -> HTTPException:
    return HTTPException(exc.status_code, {"code": exc.code, "message": str(exc)})


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


class GoogleLoginIn(BaseModel):
    code: str
    code_verifier: str
    timezone: str = "Asia/Jerusalem"


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    is_new_user: bool = False


@router.post("/auth/google", response_model=TokenOut)
async def google_login(payload: GoogleLoginIn, session: DbSession) -> TokenOut:
    try:
        identity = await exchange_code(payload.code, payload.code_verifier)
    except AuthError as exc:
        raise _handle(exc)

    from app.models import User

    existed = session.execute(
        select(User.id).where(User.google_sub == identity.sub)
    ).scalar_one_or_none()

    user = upsert_user(session, identity, payload.timezone)
    if existed is None:
        from app.models import UserStats

        session.add(UserStats(user_id=user.id))
    pair = issue_tokens(session, user)
    return TokenOut(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
        is_new_user=existed is None,
    )


class RefreshIn(BaseModel):
    refresh_token: str | None = None


@router.post("/auth/refresh", response_model=TokenOut)
def refresh(
    request: Request,
    response: Response,
    session: DbSession,
    payload: RefreshIn | None = None,
) -> TokenOut:
    """Rotate the session.

    Prefers the HttpOnly cookie set by the Google flow; the body form stays
    for non-browser clients. The rotated token goes straight back into the
    cookie so page script never has to hold it.
    """
    from app.api.auth_google import REFRESH_COOKIE, set_refresh_cookie

    supplied = (payload.refresh_token if payload else None) or request.cookies.get(
        REFRESH_COOKIE
    )
    if not supplied:
        raise HTTPException(401, {"code": "no_refresh_token"})

    try:
        pair = rotate_refresh_token(session, supplied, request.headers.get("user-agent"))
    except AuthError as exc:
        response.delete_cookie(REFRESH_COOKIE, path="/")
        raise _handle(exc)

    set_refresh_cookie(response, pair.refresh_token)
    return TokenOut(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
    )


@router.post("/auth/logout")
def logout(request: Request, response: Response, session: DbSession) -> dict:
    """Revoke the refresh token and clear the cookie."""
    from app.api.auth_google import REFRESH_COOKIE
    from app.core.security import revoke_refresh_token

    supplied = request.cookies.get(REFRESH_COOKIE)
    if supplied:
        revoke_refresh_token(session, supplied)
    response.delete_cookie(REFRESH_COOKIE, path="/")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Onboarding
# --------------------------------------------------------------------------- #


class PreferencesIn(BaseModel):
    timezone: str
    latitude: float | None = None
    longitude: float | None = None
    in_israel: bool = True
    observes_shabbat: bool = True


@router.put("/me/preferences")
def set_preferences(payload: PreferencesIn, user: CurrentUser) -> dict:
    user.timezone = payload.timezone
    user.latitude = payload.latitude
    user.longitude = payload.longitude
    user.in_israel = payload.in_israel
    user.observes_shabbat = payload.observes_shabbat
    return {"ok": True}


class PlanIn(BaseModel):
    tractate_slug: str
    daily_goal: int = Field(ge=1, le=50)
    start_date: date | None = None


class PlanOut(BaseModel):
    tractate: str
    daily_goal: int
    start_date: date
    estimated_end_date: date
    total_mishnayot: int
    calendar_days: int


@router.get("/tractates")
def list_tractates(session: DbSession) -> list[dict]:
    rows = session.execute(select(Tractate).order_by(Tractate.order_index)).scalars()
    return [
        {
            "slug": t.slug,
            "name_he": t.name_he,
            "seder": t.seder,
            "chapters": t.chapter_count,
            "mishnayot": t.mishnayot_count,
        }
        for t in rows
    ]


@router.post("/plans", response_model=PlanOut, status_code=201)
def create_plan(payload: PlanIn, user: CurrentUser, session: DbSession) -> PlanOut:
    try:
        plan, projection = progress.create_plan(
            session,
            user,
            tractate_slug=payload.tractate_slug,
            daily_goal=payload.daily_goal,
            start_date=payload.start_date,
        )
    except StudyError as exc:
        raise _handle(exc)

    tractate = session.get(Tractate, plan.tractate_id)
    return PlanOut(
        tractate=tractate.name_he,
        daily_goal=plan.daily_goal,
        start_date=plan.start_date,
        estimated_end_date=projection.estimated_end_date,
        total_mishnayot=tractate.mishnayot_count,
        calendar_days=projection.calendar_days,
    )


# --------------------------------------------------------------------------- #
# Study
# --------------------------------------------------------------------------- #


@router.get("/study/today")
def today(user: CurrentUser, session: DbSession) -> dict:
    try:
        return study.today_state(session, user)
    except StudyError as exc:
        raise _handle(exc)


class LogIn(BaseModel):
    units: int = Field(ge=1, le=50)


@router.post("/study/log")
def log_study(
    payload: LogIn,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: IdempotencyKey = None,
) -> dict:
    try:
        result = study.log_study(
            session, user, payload.units, idempotency_key=idempotency_key
        )
    except StudyError as exc:
        raise _handle(exc)
    return asdict(result)


class ShabbatReportIn(BaseModel):
    completed: bool = True


@router.post("/study/shabbat-report")
def shabbat_report(
    payload: ShabbatReportIn, user: CurrentUser, session: DbSession
) -> dict:
    try:
        result = study.report_shabbat(session, user, completed=payload.completed)
    except StudyError as exc:
        raise _handle(exc)
    return asdict(result)


@router.get("/study/history")
def history(
    user: CurrentUser,
    session: DbSession,
    since: date | None = None,
    until: date | None = None,
) -> list[dict]:
    """Feeds the streak heatmap."""
    settlement.settle_user(session, user)
    stmt = select(StudyDay).where(StudyDay.user_id == user.id)
    if since:
        stmt = stmt.where(StudyDay.local_date >= since)
    if until:
        stmt = stmt.where(StudyDay.local_date <= until)

    return [
        {
            "date": d.local_date,
            "kind": d.day_kind,
            "status": d.status,
            "required": d.required_units,
            "completed": d.completed_units,
            "points": d.points_awarded,
            "streak_after": d.streak_after,
        }
        for d in session.execute(stmt.order_by(StudyDay.local_date)).scalars()
    ]


# --------------------------------------------------------------------------- #
# Shop
# --------------------------------------------------------------------------- #


@router.get("/shop/items")
def shop_items(user: CurrentUser, session: DbSession) -> list[dict]:
    owned = {
        row.sku: row.quantity
        for row in session.execute(
            select(UserInventory).where(UserInventory.user_id == user.id)
        ).scalars()
    }
    return [
        {
            "sku": item.sku,
            "name": item.name_he,
            "description": item.description_he,
            "cost": item.cost_points,
            "owned": owned.get(item.sku, 0),
            "max_owned": item.max_owned,
        }
        for item in session.execute(
            select(ShopItem).where(ShopItem.is_active.is_(True))
        ).scalars()
    ]


class PurchaseIn(BaseModel):
    sku: str


@router.post("/shop/purchase")
def purchase(
    payload: PurchaseIn,
    user: CurrentUser,
    session: DbSession,
    idempotency_key: IdempotencyKey = None,
) -> dict:
    try:
        result = shop.purchase(
            session, user, payload.sku, idempotency_key=idempotency_key
        )
    except StudyError as exc:
        raise _handle(exc)
    return asdict(result)


@router.get("/me/transactions")
def transactions(user: CurrentUser, session: DbSession, limit: int = 50) -> list[dict]:
    rows = session.execute(
        select(PointTransaction)
        .where(PointTransaction.user_id == user.id)
        .order_by(PointTransaction.created_at.desc())
        .limit(min(limit, 200))
    ).scalars()
    return [
        {
            "amount": t.amount,
            "type": t.txn_type,
            "date": t.related_date,
            "balance_after": t.balance_after,
            "at": t.created_at,
        }
        for t in rows
    ]


# --------------------------------------------------------------------------- #
# The text - the reason the app exists
# --------------------------------------------------------------------------- #


def _portion_ordinals(session, plan, day) -> list[int]:
    """The ordinals that make up this day's portion.

    Anchored to where the day *started*, not to the current cursor, so the
    whole portion stays on screen as the learner ticks through it instead of
    vanishing mishnah by mishnah.

    The anchor counts units logged today **against this plan**, not
    `day.completed_units`. Those differ the moment someone switches tractate
    mid-day: the day row still carries the units they learned in the old
    tractate, and subtracting those would rewind the new tractate's portion by
    that many mishnayot.
    """
    logged_today = session.execute(
        select(func.coalesce(func.sum(StudyEvent.units), 0)).where(
            StudyEvent.plan_id == plan.id,
            StudyEvent.credited_local_date == day.local_date,
        )
    ).scalar_one()
    start = plan.current_ordinal - int(logged_today) + 1

    # How many to show. Normally today's requirement — but if the day row was
    # opened under a previous plan (the learner switched tractate today) it
    # carries that plan's quota, so fall back to the current goal rather than
    # previewing the wrong number of mishnayot from the new tractate.
    count = day.required_units if day.plan_id == plan.id else plan.daily_goal
    return [start + offset for offset in range(count)]


@router.get("/study/portion")
def study_portion(
    user: CurrentUser,
    session: DbSession,
    background: BackgroundTasks,
    commentaries: bool = True,
) -> dict:
    """Today's mishnayot, with text and commentaries."""
    try:
        state = study.today_state(session, user)
    except StudyError as exc:
        raise _handle(exc)

    plan = session.execute(
        select(StudyPlan).where(
            StudyPlan.user_id == user.id, StudyPlan.status == PlanStatus.ACTIVE
        )
    ).scalar_one()
    tractate = session.get(Tractate, plan.tractate_id)
    day = session.execute(
        select(StudyDay).where(
            StudyDay.user_id == user.id,
            StudyDay.local_date == state["local_date"],
        )
    ).scalar_one()

    # Settlement above may have created day rows, so this request is holding a
    # write transaction. Assembling the portion then makes up to sixteen
    # network round trips - and on SQLite a write lock held across those makes
    # every concurrent write in the process fail. Commit the business work
    # first; the text fetches below persist through their own short
    # transactions and need nothing from this one.
    session.commit()

    items = []
    for ordinal in _portion_ordinals(session, plan, day):
        if ordinal < 1 or ordinal > tractate.mishnayot_count:
            continue
        view = texts.get_mishnah(
            session, tractate, ordinal, with_commentaries=commentaries
        )
        if view is None:
            continue
        payload = texts.as_dict(view)
        payload["done"] = ordinal <= plan.current_ordinal
        items.append(payload)

    # Warm tomorrow's portion so the next open is instant.
    next_start = plan.current_ordinal + 1
    background.add_task(
        _prefetch_later,
        tractate.id,
        [next_start + i for i in range(plan.daily_goal * 2)],
    )

    return {
        "local_date": state["local_date"],
        "tractate": tractate.name_he,
        "required_units": day.required_units,
        "completed_units": day.completed_units,
        "is_double_portion": state["is_double_portion"],
        "status": day.status,
        "mishnayot": items,
    }


@router.get("/study/mishnah/{ordinal}")
def single_mishnah(
    ordinal: int, user: CurrentUser, session: DbSession, commentaries: bool = True
) -> dict:
    """Any mishnah in the active tractate - used for reviewing what came before."""
    plan = session.execute(
        select(StudyPlan).where(
            StudyPlan.user_id == user.id, StudyPlan.status == PlanStatus.ACTIVE
        )
    ).scalar_one_or_none()
    if plan is None:
        raise HTTPException(409, {"code": "no_active_plan"})

    tractate = session.get(Tractate, plan.tractate_id)
    view = texts.get_mishnah(
        session, tractate, ordinal, with_commentaries=commentaries
    )
    if view is None:
        raise HTTPException(404, {"code": "no_such_mishnah"})

    payload = texts.as_dict(view)
    payload["done"] = ordinal <= plan.current_ordinal
    payload["total"] = tractate.mishnayot_count
    return payload


def _prefetch_later(tractate_id: int, ordinals: list[int]) -> None:
    """Runs after the response is sent, in its own session."""
    from app.db.session import SessionLocal

    try:
        with SessionLocal() as session:
            tractate = session.get(Tractate, tractate_id)
            if tractate is None:
                return
            texts.prefetch(session, tractate, ordinals)
    except Exception:
        # Warming the cache is an optimisation. It must never surface to the
        # user, and it must never poison a request that already succeeded.
        logging.getLogger(__name__).warning("prefetch task failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Plan management
# --------------------------------------------------------------------------- #


def _current_plan(session, user) -> StudyPlan | None:
    return session.execute(
        select(StudyPlan).where(
            StudyPlan.user_id == user.id, StudyPlan.status == PlanStatus.ACTIVE
        )
    ).scalar_one_or_none()


class PlanUpdateIn(BaseModel):
    daily_goal: int | None = Field(default=None, ge=1, le=50)
    start_chapter: int | None = Field(default=None, ge=1)
    start_mishnah: int | None = Field(default=None, ge=1)


@router.get("/plans/current")
def get_current_plan(user: CurrentUser, session: DbSession) -> dict:
    plan = _current_plan(session, user)
    if plan is None:
        raise HTTPException(409, {"code": "no_active_plan"})
    tractate = session.get(Tractate, plan.tractate_id)
    position = session.execute(
        select(Mishnah).where(
            Mishnah.tractate_id == tractate.id,
            Mishnah.ordinal == plan.current_ordinal + 1,
        )
    ).scalar_one_or_none()

    return {
        "tractate_slug": tractate.slug,
        "tractate": tractate.name_he,
        "daily_goal": plan.daily_goal,
        "completed": plan.current_ordinal,
        "total": tractate.mishnayot_count,
        "estimated_end_date": plan.estimated_end_date,
        "next_chapter": position.chapter if position else None,
        "next_mishnah": position.number if position else None,
        "chapters": progress.chapter_structure(session, tractate),
    }


@router.put("/plans/current")
def update_current_plan(
    payload: PlanUpdateIn, user: CurrentUser, session: DbSession
) -> dict:
    """Change the daily goal and/or jump the reading position."""
    plan = _current_plan(session, user)
    if plan is None:
        raise HTTPException(409, {"code": "no_active_plan"})

    try:
        if payload.start_chapter and payload.start_mishnah:
            progress.set_position(
                session,
                plan,
                chapter=payload.start_chapter,
                mishnah=payload.start_mishnah,
            )
        goal = payload.daily_goal or plan.daily_goal
        projection = progress.change_goal(
            session, user, plan, goal, clock.now().date()
        )
    except StudyError as exc:
        raise _handle(exc)

    return {
        "daily_goal": plan.daily_goal,
        "completed": plan.current_ordinal,
        "estimated_end_date": projection.estimated_end_date,
        "calendar_days": projection.calendar_days,
    }


class SwitchIn(BaseModel):
    tractate_slug: str
    daily_goal: int = Field(ge=1, le=50)
    start_chapter: int = Field(default=1, ge=1)
    start_mishnah: int = Field(default=1, ge=1)


@router.post("/plans/switch")
def switch_plan(payload: SwitchIn, user: CurrentUser, session: DbSession) -> dict:
    """Abandon the current tractate and start another."""
    try:
        plan, projection = progress.switch_tractate(
            session,
            user,
            _current_plan(session, user),
            tractate_slug=payload.tractate_slug,
            daily_goal=payload.daily_goal,
            start_chapter=payload.start_chapter,
            start_mishnah=payload.start_mishnah,
        )
    except StudyError as exc:
        raise _handle(exc)

    tractate = session.get(Tractate, plan.tractate_id)
    return {
        "tractate": tractate.name_he,
        "daily_goal": plan.daily_goal,
        "completed": plan.current_ordinal,
        "total": tractate.mishnayot_count,
        "estimated_end_date": projection.estimated_end_date,
    }


@router.get("/tractates/{slug}/structure")
def tractate_structure(slug: str, session: DbSession) -> dict:
    tractate = session.execute(
        select(Tractate).where(Tractate.slug == slug)
    ).scalar_one_or_none()
    if tractate is None:
        raise HTTPException(404, {"code": "unknown_tractate"})
    return {
        "slug": tractate.slug,
        "name_he": tractate.name_he,
        "mishnayot": tractate.mishnayot_count,
        "chapters": progress.chapter_structure(session, tractate),
    }


# --------------------------------------------------------------------------- #
# Shas overview
# --------------------------------------------------------------------------- #

SEDER_HE = {
    "zeraim": "זרעים", "moed": "מועד", "nashim": "נשים",
    "nezikin": "נזיקין", "kodashim": "קדשים", "taharot": "טהרות",
}


@router.get("/shas")
def shas_overview(user: CurrentUser, session: DbSession) -> dict:
    """Every tractate, with how far this user has got in each.

    Progress is the furthest cursor across *all* plans for that tractate, not
    just the active one - abandoning a plan half way through Shabbat and coming
    back a year later should still show those 70 mishnayot as learned.
    """
    furthest = dict(
        session.execute(
            select(StudyPlan.tractate_id, func.max(StudyPlan.current_ordinal))
            .where(StudyPlan.user_id == user.id)
            .group_by(StudyPlan.tractate_id)
        ).all()
    )
    active = _current_plan(session, user)
    active_id = active.tractate_id if active else None

    sedarim: dict[str, dict] = {}
    for tractate in session.execute(
        select(Tractate).order_by(Tractate.order_index)
    ).scalars():
        learned = min(furthest.get(tractate.id, 0), tractate.mishnayot_count)
        bucket = sedarim.setdefault(
            tractate.seder,
            {"seder": tractate.seder, "name_he": SEDER_HE.get(tractate.seder, ""),
             "tractates": [], "learned": 0, "total": 0},
        )
        bucket["tractates"].append({
            "slug": tractate.slug,
            "name_he": tractate.name_he,
            "learned": learned,
            "total": tractate.mishnayot_count,
            "chapters": tractate.chapter_count,
            "is_active": tractate.id == active_id,
            "is_complete": learned >= tractate.mishnayot_count,
        })
        bucket["learned"] += learned
        bucket["total"] += tractate.mishnayot_count

    sedarim_list = list(sedarim.values())
    return {
        "sedarim": sedarim_list,
        "learned": sum(s["learned"] for s in sedarim_list),
        "total": sum(s["total"] for s in sedarim_list),
        "tractates_complete": sum(
            1 for s in sedarim_list for t in s["tractates"] if t["is_complete"]
        ),
    }
