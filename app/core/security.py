"""Token issuing and Google identity verification."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import RefreshToken, User


@dataclass(slots=True)
class GoogleIdentity:
    sub: str
    email: str
    email_verified: bool
    name: str | None
    picture: str | None


@dataclass(slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int


class AuthError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# Google
# --------------------------------------------------------------------------- #


async def exchange_code(code: str, code_verifier: str) -> GoogleIdentity:
    """Authorization Code + PKCE.

    The code exchange happens server-side so the client secret never reaches
    the browser, and the returned `id_token` is verified rather than trusted -
    an attacker can post any JWT they like to our endpoint.
    """
    import httpx

    settings = get_settings()
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
        )
    if response.status_code != 200:
        raise AuthError("google_exchange_failed", response.text)

    return verify_id_token(response.json()["id_token"])


def verify_id_token(raw_id_token: str) -> GoogleIdentity:
    """Verify signature, `aud`, `iss` and expiry against Google's JWKS."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    settings = get_settings()
    try:
        claims = google_id_token.verify_oauth2_token(
            raw_id_token, google_requests.Request(), settings.google_client_id
        )
    except ValueError as exc:
        raise AuthError("invalid_id_token", str(exc)) from exc

    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise AuthError("invalid_issuer", "unexpected token issuer")

    return GoogleIdentity(
        sub=claims["sub"],
        email=claims.get("email", ""),
        email_verified=bool(claims.get("email_verified")),
        name=claims.get("name"),
        picture=claims.get("picture"),
    )


def upsert_user(session: Session, identity: GoogleIdentity, timezone: str) -> User:
    """Look up by `sub`, never by email - Google emails are reassignable and
    matching on them is an account-takeover vector."""
    user = session.execute(
        select(User).where(User.google_sub == identity.sub)
    ).scalar_one_or_none()

    if user is None:
        user = User(
            google_sub=identity.sub,
            email=identity.email,
            email_verified=identity.email_verified,
            display_name=identity.name,
            avatar_url=identity.picture,
            timezone=timezone,
        )
        session.add(user)
        session.flush()
    else:
        user.email = identity.email
        user.display_name = identity.name or user.display_name
        user.avatar_url = identity.picture or user.avatar_url

    user.last_seen_at = datetime.now(UTC)
    return user


# --------------------------------------------------------------------------- #
# Our own tokens
# --------------------------------------------------------------------------- #


def issue_tokens(
    session: Session, user: User, user_agent: str | None = None
) -> TokenPair:
    settings = get_settings()
    now = datetime.now(UTC)
    ttl = timedelta(minutes=settings.access_token_ttl_minutes)

    access = jwt.encode(
        {
            "sub": str(user.id),
            "iat": int(now.timestamp()),
            "exp": int((now + ttl).timestamp()),
            "typ": "access",
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    raw_refresh = secrets.token_urlsafe(48)
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=_hash(raw_refresh),
            expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
            user_agent=(user_agent or "")[:400] or None,
        )
    )
    return TokenPair(access, raw_refresh, int(ttl.total_seconds()))


def rotate_refresh_token(
    session: Session, raw_refresh: str, user_agent: str | None = None
) -> TokenPair:
    """Single-use refresh tokens. Reusing a consumed one is the classic signal
    of a stolen token, so it revokes the whole chain for that user."""
    now = datetime.now(UTC)
    record = session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == _hash(raw_refresh))
    ).scalar_one_or_none()

    if record is None:
        raise AuthError("invalid_refresh", "unknown refresh token")
    if record.revoked_at is not None:
        session.execute(
            RefreshToken.__table__.update()
            .where(
                RefreshToken.user_id == record.user_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        raise AuthError("refresh_reuse", "refresh token replay detected; re-login")
    if record.expires_at <= now:
        raise AuthError("expired_refresh", "refresh token expired")

    user = session.get(User, record.user_id)
    pair = issue_tokens(session, user, user_agent)
    record.revoked_at = now
    session.flush()
    return pair


def revoke_refresh_token(session: Session, raw_refresh: str) -> None:
    """Best-effort logout. An unknown token is already not a session."""
    record = session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == _hash(raw_refresh))
    ).scalar_one_or_none()
    if record is not None and record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)


def decode_access_token(token: str) -> uuid.UUID:
    settings = get_settings()
    try:
        claims = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except jwt.PyJWTError as exc:
        raise AuthError("invalid_token", str(exc)) from exc
    if claims.get("typ") != "access":
        raise AuthError("wrong_token_type", "expected an access token")
    return uuid.UUID(claims["sub"])


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
