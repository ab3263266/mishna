"""Refuse to start with a configuration that is unsafe on a public host.

These are all mistakes that produce a *working* app — nothing breaks, no test
fails, and the damage is invisible until someone finds it. That is exactly the
class of mistake worth failing loudly at boot.
"""

from __future__ import annotations

import logging

from app.core.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_SECRET = "dev-only-secret-not-for-production-use"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0")


class UnsafeConfiguration(RuntimeError):
    pass


def _is_local(url: str) -> bool:
    return any(host in url for host in LOCAL_HOSTS)


def check(settings: Settings) -> list[str]:
    """Returns warnings. Raises on anything that must not reach the internet."""
    public = not _is_local(settings.public_base_url)
    fatal: list[str] = []
    warnings: list[str] = []

    if settings.dev_mode and public:
        fatal.append(
            f"DEV_MODE is on but PUBLIC_BASE_URL is {settings.public_base_url!r}. "
            "/dev/login mints a session for any email address and /dev/time moves "
            "the clock for every user. Set DEV_MODE=false."
        )

    if public and settings.jwt_secret == DEFAULT_SECRET:
        fatal.append(
            "JWT_SECRET is still the built-in default, so anyone can forge an "
            "access token for any account. Set it to 32+ random bytes."
        )

    if public and not settings.cookie_secure:
        fatal.append(
            "COOKIE_SECURE is false on a public host, so the session cookie "
            "would be sent over plain HTTP."
        )

    if public and settings.database_url.startswith("sqlite"):
        warnings.append(
            "Running on SQLite. Most hosts give containers an ephemeral disk, so "
            "every deploy would wipe user progress - and SQLite has no row locks, "
            "which the streak logic relies on under concurrency. Use PostgreSQL."
        )

    if public and not (settings.owner_name and settings.contact_email):
        warnings.append(
            "OWNER_NAME / CONTACT_EMAIL are unset, so the privacy policy, the "
            "accessibility statement and the copyright notice all name nobody "
            "and give no address to write to. Those pages exist to be acted "
            "on; set both."
        )

    if public and not settings.google_client_id:
        warnings.append(
            "GOOGLE_CLIENT_ID is unset, so email and password is the only way "
            "in. That works; set it if you also want the Google button."
        )

    if settings.google_client_id and not settings.google_redirect_uri.startswith(
        settings.public_base_url.rstrip("/")
    ):
        warnings.append(
            f"GOOGLE_REDIRECT_URI ({settings.google_redirect_uri!r}) is not under "
            f"PUBLIC_BASE_URL ({settings.public_base_url!r}); Google will reject "
            "the callback."
        )

    if fatal:
        raise UnsafeConfiguration(
            "refusing to start:\n  - " + "\n  - ".join(fatal)
        )
    for warning in warnings:
        logger.warning("config: %s", warning)
    return warnings
