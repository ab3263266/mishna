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
- **The only calendar rule is `User.study_week`**: seven days, or five
  (Sunday–Thursday, with Friday and Shabbat as rest days). There is no zmanim,
  no Hebrew calendar, and no special handling of Shabbat anywhere else. A rest
  day requires nothing and holds the streak, but still credits if the learner
  studies anyway.
- **Points move only through `ledger.post_transaction`**, with a deterministic
  `idempotency_key`. A `None` return means "already applied" — do not move the
  balance again.
- **Nothing on a request path touches the network.** The texts are files.
- **Sign-in must not write profile fields.** Two accepted ways in — email and
  password (scrypt, stdlib) or Google — and neither may touch `study_week`,
  `daily_goal` or anything else the learner chose. A login handler that
  defaults a field silently resets it on every visit; that already happened
  once.
- **Login failures say one thing.** A wrong password and an unknown address
  return the identical body *and* take the same time, or the form becomes a
  directory of who has an account.

## The texts are data, not a service

`app/data/texts/<slug>.json.gz` — 63 committed files, ~11 MB, one per tractate,
produced by `scripts/fetch_texts.py` and read straight off disk by
`app/services/texts.py`. Do not reintroduce a live Sefaria call or a cache
table: the study screen has to render on a train.

Two traps if you ever re-run the fetch script:

- **A commentary's numbering is not the mishnah's.** `Yachin on Mishnah
  Berakhot 1:13` is the thirteenth comment *in chapter 1*, which lands on
  mishnah 2. Anchoring must go through Sefaria's `/api/links` graph. Indexing
  by position returns wrong text with no error.
- **No single Sefaria edition is complete**, and the biggest books time out.
  The script layers editions and falls back to chapter-by-chapter fetching;
  `tests/test_texts.py` checks the committed result rather than trusting it.

`mishnayot.ordinal` is the join between the database and those files. If the
two disagree, learners are shown a different mishnah from the one their
progress says they are on.

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
