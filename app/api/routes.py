"""HTTP surface.

Thin by design: every handler validates input, calls exactly one service, and
serialises the result. No business rule lives here - that is what makes the
rules testable without spinning up an app.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, IdempotencyKey
from app.core.security import AuthError, exchange_code, issue_tokens, rotate_refresh_token, upsert_user
from app.models import (
    PlanStatus,
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
    refresh_token: str


@router.post("/auth/refresh", response_model=TokenOut)
def refresh(payload: RefreshIn, session: DbSession) -> TokenOut:
    try:
        pair = rotate_refresh_token(session, payload.refresh_token)
    except AuthError as exc:
        raise _handle(exc)
    return TokenOut(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
    )


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


def _portion_ordinals(plan, day) -> list[int]:
    """The ordinals that make up this day's portion.

    Anchored to where the day *started*, not to the current cursor, so the
    whole portion stays on screen as the learner ticks through it instead of
    vanishing mishnah by mishnah.
    """
    start = plan.current_ordinal - day.completed_units + 1
    return [start + offset for offset in range(day.required_units)]


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

    items = []
    for ordinal in _portion_ordinals(plan, day):
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

    with SessionLocal() as session:
        tractate = session.get(Tractate, tractate_id)
        if tractate is None:
            return
        texts.prefetch(session, tractate, ordinals)
        session.commit()
