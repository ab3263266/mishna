# Mishnah Tracker

Daily Mishnah study tracker: the text of today's mishnayot with their
commentaries, plus streaks and points around it. FastAPI + SQLAlchemy 2.0,
SQLite locally and PostgreSQL in production. Hebrew RTL UI.

**Unrelated to any other project on this machine.** `Desktop\אתר` is a separate
static site (Epoxy Time) with its own repo — never put files there.

## Commands

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8010   # run
.venv/Scripts/python.exe -m pytest tests/ -q                            # test
.venv/Scripts/python.exe -m app.db.seed                                 # reseed
.venv/Scripts/python.exe demo.py                                        # rules, printed
```

The venv already exists. First boot creates `mishnah.db` and seeds 63 tractates
/ 4,192 mishnayot from the committed `app/data/tractates.json` — no network
needed. `.env` turns on `DEV_MODE` locally; it is gitignored and must stay that
way.

## Read `README.md` before changing behaviour

It documents why the design is shaped the way it is. The short version of what
will bite you:

- **`settle_user` owns every streak and points transition.** Nothing else may
  touch them. It is idempotent, resolves days in order, and never finalises
  *today*.
- **Terminal day statuses are never recomputed.** That property is what makes
  settlement safe to run from a request and a cron simultaneously.
- **A day is a local date with a 03:00 rollover**, not a UTC date. Always go
  through `UserClock`; never call `.date()` on a timestamp.
- **Shabbat holds the streak, it does not advance it.** The difference between
  "unchanged" and "reset to 0" is the entire Sabbath-mode feature.
- **Friday credits before Shabbat.** The second day scores at the tier the
  first just unlocked.
- **Never do network I/O inside a write transaction.** This already caused
  "database is locked" once: `/study/portion` settled (a write) and then made
  16 Sefaria calls with the transaction open. Commit the business work first;
  the text cache writes through its own short transactions.
- **Points move only through `ledger.post_transaction`**, with a deterministic
  `idempotency_key`. A `None` return means "already applied" — do not move the
  balance again.

## Conventions

- Business rules live in `app/services/`, never in `app/api/`. Handlers
  validate, call one service, serialise.
- `app/services/scoring.py` is pure — no database, no clock. Keep it that way;
  it is what makes the economy testable.
- Read the time through `app.core.clock.now()`, never `datetime.now()`, so
  tests and the dev time-travel bar work.
- Sefaria refs are built from `tractates.sefaria_title`, not `name_en`
  (Sefaria files Uktzin as *Oktzin*).
- Tests must pass before committing. Anything that reached the running app as
  a bug gets a regression test — see `tests/test_portion.py`.

## Deployment

`DEV_MODE` defaults **off** and must stay off in production: `/dev/login`
mints a session for any email address. `app/core/preflight.py` refuses to boot
on an unsafe config. Alembic owns the production schema; `create_all` is for
local SQLite only. See README for the full deployment guide.
