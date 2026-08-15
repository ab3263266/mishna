"""Email + password sign-in.

The hashing tests are pure. The endpoint tests run against a real app and a
real database, because the parts worth pinning down here — that a duplicate
address is refused, that a wrong password and an unknown address are
indistinguishable — live in the handler, not in the crypto.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.security import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    verify_password,
)
from app.models import Base, User


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #


def test_a_password_verifies_against_its_own_hash() -> None:
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("Correct horse battery staple", stored)
    assert not verify_password("", stored)


def test_the_same_password_hashes_differently_every_time() -> None:
    """A per-password salt is what stops one leaked rainbow table from opening
    every account that chose the same weak password."""
    first = hash_password("hunter2hunter2")
    second = hash_password("hunter2hunter2")
    assert first != second
    assert verify_password("hunter2hunter2", first)
    assert verify_password("hunter2hunter2", second)


def test_the_hash_carries_its_parameters() -> None:
    """Stored as scheme$n$r$p$salt$hash, so the cost can be raised later
    without invalidating every existing password."""
    scheme, n, r, p, salt, digest = hash_password("something long enough").split("$")
    assert scheme == "scrypt"
    assert int(n) >= 2**14 and int(r) >= 8 and int(p) >= 1
    assert salt and digest


def test_an_account_with_no_password_never_verifies() -> None:
    """Google accounts have `password_hash = NULL`. Any answer but False here
    would let an empty string sign in as them."""
    assert not verify_password("", None)
    assert not verify_password("anything", None)
    assert not verify_password("anything", "")


@pytest.mark.parametrize("corrupt", [
    "not-a-hash",
    "scrypt$16384$8$1$onlyfourparts",
    "bcrypt$16384$8$1$c2FsdA==$aGFzaA==",   # a scheme we do not implement
])
def test_a_corrupt_stored_hash_fails_closed(corrupt: str) -> None:
    assert not verify_password("anything", corrupt)


# --------------------------------------------------------------------------- #
# The endpoints
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(monkeypatch) -> TestClient:
    # StaticPool, because TestClient serves requests on another thread and the
    # default pool would hand it a *different* :memory: database - one with no
    # tables in it.
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)

    from app.api.deps import DbSession  # noqa: F401
    from app.db.session import get_session
    from app.main import app

    def override():
        session = TestingSession()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    app.dependency_overrides[get_session] = override
    with TestClient(app) as test_client:
        test_client.engine = engine
        test_client.sessionmaker = TestingSession
        yield test_client
    app.dependency_overrides.clear()


def register(client, email="learner@example.com", password="a-good-password"):
    return client.post("/api/v1/auth/register",
                       json={"email": email, "password": password})


def test_registration_creates_an_account_and_signs_it_in(client) -> None:
    response = register(client)
    assert response.status_code == 201
    body = response.json()
    assert body["is_new_user"] and body["access_token"]

    me = client.get("/api/v1/me",
                    headers={"Authorization": "Bearer " + body["access_token"]})
    assert me.status_code == 200
    assert me.json()["email"] == "learner@example.com"
    assert me.json()["sign_in"] == "password"


def test_registration_starts_the_stats_row(client) -> None:
    """Without it the first study log has nothing to lock and 500s."""
    from app.models import UserStats

    register(client)
    with client.sessionmaker() as session:
        user = session.execute(select(User)).scalar_one()
        assert session.get(UserStats, user.id) is not None


def test_the_password_is_never_stored_in_the_clear(client) -> None:
    register(client, password="plaintext-would-be-fatal")
    with client.sessionmaker() as session:
        stored = session.execute(select(User.password_hash)).scalar_one()
    assert "plaintext-would-be-fatal" not in stored
    assert stored.startswith("scrypt$")


def test_a_short_password_is_refused(client) -> None:
    response = register(client, password="a" * (MIN_PASSWORD_LENGTH - 1))
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "password_too_short"


def test_the_same_address_cannot_register_twice(client) -> None:
    assert register(client).status_code == 201
    duplicate = register(client, password="a-different-password")
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "email_taken"


def test_the_address_check_ignores_case(client) -> None:
    """Otherwise Learner@ and learner@ are two accounts, and the second one
    silently starts from zero."""
    register(client, email="Learner@Example.com")
    assert register(client, email="learner@example.com").status_code == 409


def test_login_accepts_the_right_password(client) -> None:
    register(client)
    response = client.post("/api/v1/auth/login",
                           json={"email": "learner@example.com",
                                 "password": "a-good-password"})
    assert response.status_code == 200
    assert response.json()["access_token"]


def test_login_normalises_the_address(client) -> None:
    register(client, email="learner@example.com")
    response = client.post("/api/v1/auth/login",
                           json={"email": "  LEARNER@example.com  ",
                                 "password": "a-good-password"})
    assert response.status_code == 200


def test_a_wrong_password_and_an_unknown_address_are_indistinguishable(client) -> None:
    """Different answers would turn the login form into a list of who has an
    account here."""
    register(client)
    wrong = client.post("/api/v1/auth/login",
                        json={"email": "learner@example.com", "password": "nope"})
    unknown = client.post("/api/v1/auth/login",
                          json={"email": "nobody@example.com", "password": "nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_a_google_account_cannot_be_signed_into_with_a_password(client) -> None:
    with client.sessionmaker() as session:
        session.add(User(google_sub=f"google-{uuid.uuid4()}",
                         email="google-user@example.com", timezone="Asia/Jerusalem"))
        session.commit()

    for password in ("", "guess", "null"):
        response = client.post("/api/v1/auth/login",
                               json={"email": "google-user@example.com",
                                     "password": password})
        assert response.status_code == 401


def test_registration_sets_the_refresh_cookie_so_a_reload_stays_signed_in(client) -> None:
    from app.api.auth_google import REFRESH_COOKIE

    register(client)
    assert client.cookies.get(REFRESH_COOKIE)
    assert client.post("/api/v1/auth/refresh").status_code == 200


def test_logout_revokes_the_session(client) -> None:
    register(client)
    assert client.post("/api/v1/auth/logout").json() == {"ok": True}
    assert client.post("/api/v1/auth/refresh").status_code == 401
