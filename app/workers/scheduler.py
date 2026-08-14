"""In-process hourly settlement sweep.

The sweep exists only for users who stop opening the app: `settle_user` already
runs on every read, so an active user is always current. It matters for the
lapsed ones, whose penalties would otherwise never be recorded and whose streak
would sit frozen at 40 forever.

Running it inside the web process rather than as a separate scheduled service
is a deliberate trade:

* One service instead of two. No managed-cron product to pay for, and one set
  of environment variables to keep in sync.
* If the process is asleep the sweep does not run — but a sleeping process has
  no users, and settlement is idempotent and catches up the moment anyone
  reads. The invariant survives; only its timeliness slips.
* With several web replicas every replica sweeps. That is wasteful, not
  wrong — the unique `idempotency_key` on each award means the duplicates are
  discarded by the database.

If you outgrow that (many replicas, or you want the sweep to run while the web
tier is scaled to zero), set `RUN_SCHEDULER=false` and run
`python -m app.workers.nightly` from a real scheduler instead. The worker is
the same code either way.
"""

from __future__ import annotations

import asyncio
import logging

from app.workers.nightly import run

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 3600
#: Let the app finish starting before the first sweep competes for the database.
STARTUP_DELAY_SECONDS = 60


async def _loop() -> None:
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            # `run` is synchronous and database-bound; a thread keeps it off the
            # event loop so requests are not blocked while it sweeps.
            summary = await asyncio.to_thread(run)
            if summary["processed"]:
                logger.info("settlement sweep %s", summary)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed sweep must never take the web process down with it.
            logger.exception("settlement sweep failed; retrying next interval")
        await asyncio.sleep(INTERVAL_SECONDS)


def start(app) -> asyncio.Task | None:
    from app.core.config import get_settings

    if not get_settings().run_scheduler:
        logger.info("in-process scheduler disabled")
        return None

    task = asyncio.create_task(_loop(), name="settlement-sweep")
    logger.info("settlement sweep scheduled every %ss", INTERVAL_SECONDS)
    return task


async def stop(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
