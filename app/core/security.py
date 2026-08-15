"""Password hashing, token issuing, and Google identity verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache

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
# Passwords
# --------------------------------------------------------------------------- #

#: scrypt (RFC 7914) from the standard library, rather than bcrypt or argon2
#: from a compiled wheel. It is memory-hard, it is what `hashlib` gives you for
#: free, and it keeps this app installable with no build toolchain.
#:
#: n=2**14 with r=8 is 16 MB and ~100ms per hash - the RFC's interactive
#: parameter, slow enough to make an offline attack on a leaked database
#: expensive and fast enough that nobody notices it on a login. Going higher
#: needs an explicit `maxmem`: OpenSSL refuses anything over 32 MB by default,
#: so n=2**15 raises "memory limit exceeded" rather than being merely slower.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$hash`, all base64. The parameters travel with the
    digest so they can be raised later without invalidating existing ones."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32,
    )
    encode = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${encode(salt)}${encode(digest)}"


def _as_utc(moment: datetime) -> datetime:
    """Read a stored instant back as timezone-aware.

    Everything is written as aware UTC, but SQLite has no timezone type and
    hands the value back naive - so comparing it against `now()` raises
    TypeError instead of answering the question. Postgres does not have this
    problem, which is exactly why it is worth pinning: the failure only ever
    shows up in development, on the refresh path, after the access token has
    been dropped.
    """
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A hash of a value nobody knows, used to spend the same time on a login
    for an address that has no account. Built lazily so the ~0.3s of scrypt is
    not paid at import time by processes that never see a login."""
    return hash_password(secrets.token_urlsafe(32))


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check. False for an account with no password at all, so a
    Google-only account cannot be signed into with an empty string.

    A missing hash still does the work against a dummy: returning early would
    make "no such account" measurably faster than "wrong password", which turns
    the login form into an oracle for who has an account here.
    """
    if not stored:
        verify_password(password, _dummy_hash())
        return False
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p),
            dklen=len(base64.b64decode(digest_b64)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, base64.b64decode(digest_b64))


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
    if _as_utc(record.expires_at) <= now:
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
