from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.core.security import AuthError, decode_access_token
from app.db.session import get_session
from app.models import User

DbSession = Annotated[Session, Depends(get_session)]


def current_user(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, {"code": "missing_token"})
    try:
        user_id = decode_access_token(authorization.split(" ", 1)[1])
    except AuthError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": str(exc)})

    user = session.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise HTTPException(401, {"code": "unknown_user"})
    return user


CurrentUser = Annotated[User, Depends(current_user)]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
