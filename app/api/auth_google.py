"""Google sign-in, browser redirect flow.

Authorization Code + PKCE, driven entirely from the server. The alternative -
handing the SPA a client secret, or letting it post a Google `id_token` we
trust - are both ways to hand out sessions to anyone who asks.

Session handling:

* The **refresh token** goes into an HttpOnly, Secure, SameSite=Lax cookie.
  Script on the page cannot read it, so an XSS bug cannot walk off with a
  30-day session.
* The **access token** is short-lived and returned in the response body for
  the SPA to keep in memory. On load the page calls `/auth/refresh`, which
  reads the cookie.

The PKCE verifier and the CSRF `state` are carried in a second short-lived
signed cookie rather than server-side storage, so this works with more than
one process and needs no shared cache.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.api.deps import DbSession
from app.core.config import get_settings
from app.core.security import AuthError, exchange_code, issue_tokens, upsert_user
from app.models import User, UserStats

router = APIRouter(prefix="/auth/google", tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REFRESH_COOKIE = "mishnah_refresh"
FLOW_COOKIE = "mishnah_oauth_flow"
FLOW_TTL_SECONDS = 600


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


def set_refresh_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.refresh_token_ttl_days * 24 * 3600,
        path="/",
    )


@router.get("/login")
def google_login_redirect(request: Request) -> RedirectResponse:
    """Send the browser to Google's consent screen."""
    settings = get_settings()
    if not settings.google_client_id:
        raise HTTPException(503, {"code": "google_not_configured",
                                  "message": "GOOGLE_CLIENT_ID is not set"})

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "online",
        "prompt": "select_account",
    }
    response = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")

    flow = jwt.encode(
        {
            "state": state,
            "verifier": verifier,
            "tz": request.query_params.get("tz", "Asia/Jerusalem"),
            "exp": int(
                (datetime.now(UTC) + timedelta(seconds=FLOW_TTL_SECONDS)).timestamp()
            ),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    response.set_cookie(
        FLOW_COOKIE, flow, httponly=True, secure=settings.cookie_secure,
        samesite="lax", max_age=FLOW_TTL_SECONDS, path="/",
    )
    return response


@router.get("/callback")
async def google_callback(
    request: Request,
    session: DbSession,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    settings = get_settings()
    landing = settings.public_base_url.rstrip("/")

    if error:
        return RedirectResponse(f"{landing}/?auth_error={error}")
    if not code or not state:
        return RedirectResponse(f"{landing}/?auth_error=missing_code")

    raw_flow = request.cookies.get(FLOW_COOKIE)
    if not raw_flow:
        return RedirectResponse(f"{landing}/?auth_error=flow_expired")

    try:
        flow = jwt.decode(
            raw_flow, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except jwt.PyJWTError:
        return RedirectResponse(f"{landing}/?auth_error=flow_invalid")

    # CSRF check: the state Google echoed back must match the one we issued.
    if not secrets.compare_digest(str(flow.get("state", "")), state):
        return RedirectResponse(f"{landing}/?auth_error=state_mismatch")

    try:
        identity = await exchange_code(code, flow["verifier"])
    except AuthError:
        return RedirectResponse(f"{landing}/?auth_error=exchange_failed")

    if not identity.email_verified:
        # An unverified Google address is not proof of ownership.
        return RedirectResponse(f"{landing}/?auth_error=email_unverified")

    existed = session.execute(
        select(User.id).where(User.google_sub == identity.sub)
    ).scalar_one_or_none()
    user = upsert_user(session, identity, flow.get("tz", "Asia/Jerusalem"))
    if existed is None:
        session.add(UserStats(user_id=user.id))
    session.flush()

    pair = issue_tokens(session, user, request.headers.get("user-agent"))
    response = RedirectResponse(f"{landing}/")
    set_refresh_cookie(response, pair.refresh_token)
    response.delete_cookie(FLOW_COOKIE, path="/")
    return response


@router.get("/configured")
def google_configured() -> dict:
    """Lets the UI show the right sign-in button without guessing."""
    settings = get_settings()
    return {
        "google": bool(settings.google_client_id),
        "dev_mode": settings.dev_mode,
    }
