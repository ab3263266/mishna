"""The application clock.

Every service reads the time through `clock.now()` rather than calling
`datetime.now()` directly. That single indirection buys two things:

* tests can drive the engine through a month in milliseconds;
* the dev UI can time-travel, which is the only practical way to *see* a
  7-day streak or a Shabbat freeze without waiting a week.

The offset is process-local and only mutable when `dev_mode` is on. Auth token
expiry deliberately does NOT use this clock - time-travelling your own session
into an expired state is a confusing way to find out the feature works.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

_lock = threading.Lock()
_offset = timedelta(0)


def now() -> datetime:
    """Current instant, UTC, plus any dev offset."""
    return datetime.now(UTC) + _offset


def offset() -> timedelta:
    return _offset


def shift(delta: timedelta) -> timedelta:
    """Move the clock. Dev only."""
    global _offset
    with _lock:
        _offset += delta
        return _offset


def reset() -> None:
    global _offset
    with _lock:
        _offset = timedelta(0)
