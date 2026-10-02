"""Async SQLite layer. (company, title) is the dedupe key; status/quota enforced in SQL."""
import asyncio
import json
import sqlite3
import aiosqlite
from contextlib import asynccontextmanager
from typing import Any, Iterable, Sequence

import config
import parsing

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    job_url     TEXT NOT NULL UNIQUE,
    platform    TEXT NOT NULL CHECK (platform IN {config.PLATFORMS}),
    salary      TEXT,
    bond_terms  TEXT,
    status      TEXT NOT NULL DEFAULT 'new' CHECK (status IN {config.JOB_STATUSES}),
    hr_email    TEXT,
    recruiter_linkedin_url TEXT,
    description TEXT,
    pdf_path    TEXT,
    cli_session_id TEXT,
    date_posted TEXT,
    applied_at  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_applied_at ON jobs(applied_at);

-- Named Claude CLI chat sessions. The CLI has no notion of a session *name* - `--resume` takes a
-- UUID it minted - so the name lives here and maps to that UUID. One row ('claude-session') is the
-- whole point: every job appends to the same conversation instead of paying for a cold context.
CREATE TABLE IF NOT EXISTS cli_sessions (
    name       TEXT PRIMARY KEY,
    session_id TEXT,
    turns      INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT
);

-- Permanent HR-mailbox cache, keyed per employer. Deliberately NOT part of the job queue: the queue
-- is disposable (clear_all_jobs wipes it every day) while a discovered mailbox cost a paid Hunter
-- credit, a browser session or a human login to find, and is still valid next month. clear_all_jobs
-- only DELETEs FROM jobs, so this table survives every wipe - see the assertion in _selfcheck.
CREATE TABLE IF NOT EXISTS email_vault (
    domain        TEXT PRIMARY KEY,
    email         TEXT NOT NULL,
    source        TEXT,
    discovered_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Cross-run "we already approached this employer" ledger. Like email_vault it lives outside jobs so
-- the daily wipe (which only DELETEs FROM jobs) cannot forget it: one application per company per
-- COMPANY_COOLDOWN_DAYS, even after the queue that produced it is gone. company_norm mirrors the
-- LOWER(TRIM(company)) used by the dedupe index so the same firm can never occupy two rows.
CREATE TABLE IF NOT EXISTS company_history (
    company_norm   TEXT PRIMARY KEY,
    last_action_at TEXT,
    kind           TEXT
);

-- The daily build -> GitHub -> LinkedIn ledger, written by social.py and read by the dashboard.
-- (day, kind) IS the once-a-day gate: the second click on one day finds today's row and reports it
-- instead of building a second repository, and a failed attempt is rewritten in place rather than
-- piling up rows. kind is 'announce' for the day a project is pitched, 'phase' for each day of it that
-- ships, 'finish' for the wrap-up, and 'profile' for the 15-day GitHub profile refresh, which keeps
-- its own clock in the same table and one read for both.
CREATE TABLE IF NOT EXISTS daily_posts (
    day        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    title      TEXT,
    summary    TEXT,
    slug       TEXT,
    body       TEXT,
    repo_name  TEXT,
    repo_url   TEXT,
    image_path TEXT,
    status     TEXT NOT NULL,
    error      TEXT,
    posted_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (day, kind)
);

-- The project one press a day is currently building. One row at a time, finished ones kept as history:
-- the slug is the repository name, so `files` is what stops a phase from re-pushing files that have not
-- changed, `output` is the real stdout of the last run (the proof the post went out with), and `done`
-- counts the phases that actually ran and shipped. `run` is the one command every phase has to survive
-- and `intro` is the announcement as it was written, both kept so that the retry lane can replay the
-- day without asking the model for the same plan twice. announced=0 is the retry lane: the repository
-- and the card can both already exist when the post is what failed, and that press must finish the
-- announcement instead of creating a second repository for the same project.
CREATE TABLE IF NOT EXISTS projects (
    slug         TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    pitch        TEXT NOT NULL DEFAULT '',
    tech         TEXT NOT NULL DEFAULT '',
    run          TEXT NOT NULL DEFAULT 'python main.py',
    intro        TEXT NOT NULL DEFAULT '',
    phases       TEXT NOT NULL DEFAULT '[]',
    done         INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL,
    announced    INTEGER NOT NULL DEFAULT 0,
    repo_url     TEXT NOT NULL DEFAULT '',
    files        TEXT NOT NULL DEFAULT '{{}}',
    output       TEXT NOT NULL DEFAULT '',
    headline     TEXT NOT NULL DEFAULT '',
    about        TEXT NOT NULL DEFAULT '',
    started_day  TEXT NOT NULL,
    finished_day TEXT NOT NULL DEFAULT ''
);
"""

CLAUDE_SESSION = "claude-session"     # the one named session the whole pipeline appends to

_COLUMNS = ("title", "company", "job_url", "platform", "salary", "bond_terms", "status",
            "hr_email", "recruiter_linkedin_url", "description", "date_posted", "fit_score", "track",
            "role")
# Additive migration for older jobs.db files: cli_session_id records which CLI conversation produced
# this row, date_posted lets parsing.eligible re-check the freshness gate on a row read back from the
# DB, recruiter_linkedin_url is the outreach fallback when no HR mailbox resolves. notes is the last
# run's verdict for both halves - without it a failure's REASON lives only in terminal scrollback, so
# diagnosing one means re-running the job, and a re-run re-mails the recruiter.
_LATE_COLUMNS = {"description": "TEXT", "pdf_path": "TEXT", "cli_session_id": "TEXT",
                 "date_posted": "TEXT", "recruiter_linkedin_url": "TEXT", "outreach_at": "TEXT",
                 "notes": "TEXT", "email_msgid": "TEXT", "replied_at": "TEXT",
                 "followup_at": "TEXT", "fit_score": "REAL", "track": "TEXT", "template": "TEXT",
                 # Which of config.ROLES this req belongs to (parsing.role_of). Rows scraped before the
                 # column existed are backfilled in init_db, so the dashboard's role filter covers the
                 # whole queue and not only the hauls that arrived after this shipped.
                 "role": "TEXT",
                 # The AI's copy for this job, kept so a run that was interrupted after the resume was
                 # already built can pick it back up without paying for the same generation twice.
                 "assets_json": "TEXT",
                 # Which halves the last run tried and did not land: "apply", "outreach", "resume", or a
                 # comma-separated few. The status label cannot carry this. A submitted row whose outreach
                 # reached nobody is 'applied' - the strongest true thing about it, and the whole point of
                 # the red 'you have not applied' alert being built from facts - so the label says nothing
                 # about the half that broke, and without this column the card would show the outreach
                 # failure NOWHERE: a green apply, a green resume, and a red retry button with no reason
                 # printed next to it. Written by every terminal verdict of a run; empty means nothing
                 # broke, and a later run that lands the half clears it by writing its own list.
                 "phase_fail": "TEXT"}

# Identity is (company, title), NOT the URL. Every board decorates a posting link with per-impression
# tracking tokens and re-serves the *same* req as a sponsored copy under a different path, so
# `job_url UNIQUE` let 12 identical Accenture rows through while rejecting nothing that mattered.
# job_url stays UNIQUE underneath as a cheap second net; this is the gate that actually holds.
_FINGERPRINT = "SELECT id FROM jobs WHERE LOWER(title) = LOWER(?) AND LOWER(company) = LOWER(?)"
_INSERT = (f"INSERT OR IGNORE INTO jobs ({','.join(_COLUMNS)})"
           f" VALUES ({','.join('?' * len(_COLUMNS))})")
# One expression, used by both the sweep and the index it creates - if they ever disagreed, the
# DELETE would leave a row the index then refuses to be created over.
_DEDUPE_KEY = "LOWER(TRIM(company)), LOWER(TRIM(title))"

# A row is IRREVERSIBLE once it left this machine: applied_at means it is in that employer's ATS, and
# outreach_at means a recruiter has the mail. Neither fact can be undone, so neither row may be
# silently forgotten - the wipe and the filters below are built on this predicate.
_LEFT_THE_MACHINE = "applied_at IS NOT NULL OR outreach_at IS NOT NULL"

# Named views the queue can be filtered by, on top of the status filter. Whole SQL predicates, never
# assembled from input, so a request can only ever select a key that exists in this dict.
# The day strip first, because that is the axis the queue is actually worked on:
#   recent    - rows scraped TODAY. The day the operator is working, and empty when today's scrape has
#               not run yet - which is the honest answer, not "the newest day in the table", because a
#               tab that silently shows yesterday's haul under today's name is a tab you cannot trust
#               to tell you whether the scrape ran.
#   d1..d5    - the scraped day N days back. Together with 'recent' these six tabs are the whole life
#               of a scrape: config.SCRAPE_KEEP_DAYS days, retired on the morning of the next one, so
#               the oldest tab lists exactly the rows the purge is about to take.
# Then the funnel views:
#   followups - mailed or DMed, unanswered: the rows a nudge is coming for, on either channel
#   dms       - reached over LinkedIn only (no mailbox on file), the rows whose nudge goes back out on
#               LinkedIn rather than by mail
#   notapplied- the work still owed: either half of the pair missing. A row whose application never
#               landed but whose mail went out reads as 'emailed' - true about the outreach, false about
#               the job - and nothing else in the app would ever surface it again, since every queue
#               fetches status='queued' and that row left 'queued' long ago. Fact-based, not
#               status-based, for the same reason the facts are columns and the status is only a
#               verdict. 'skipped' is excluded: an ineligible row is out on purpose, not owed.
# created_at is already local (its default is datetime('now','localtime')), so no conversion: the
# scrape day IS the calendar day the operator scraped on, midnight to midnight, not a UTC approximation.
_SCRAPED_ON = "date(created_at)"
_VIEWS = {"recent": f"{_SCRAPED_ON} = date('now','localtime')",
          "d1": f"{_SCRAPED_ON} = date('now','localtime','-1 day')",
          "d2": f"{_SCRAPED_ON} = date('now','localtime','-2 day')",
          "d3": f"{_SCRAPED_ON} = date('now','localtime','-3 day')",
          "d4": f"{_SCRAPED_ON} = date('now','localtime','-4 day')",
          "d5": f"{_SCRAPED_ON} = date('now','localtime','-5 day')",
          "followups": "outreach_at IS NOT NULL AND replied_at IS NULL",
          "dms": "outreach_at IS NOT NULL AND hr_email IS NULL AND replied_at IS NULL",
          "notapplied": "status <> 'skipped' AND (applied_at IS NULL OR outreach_at IS NULL)"}
VIEWS = tuple(_VIEWS)          # the allowed view names, for a route to validate a query against


@asynccontextmanager
async def connect(db_path=None):
    db = await aiosqlite.connect(db_path or config.DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    # Root fix for "database is locked": WAL lets readers run during a write, but two writers still
    # collide - a scrape inserting while the kill-switch deletes. busy_timeout makes the loser wait for
    # the lock instead of raising instantly, and it applies to every statement in the app, not just the
    # wipe. 5s is far longer than any statement here takes; a real deadlock still surfaces as an error.
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        await db.commit()
    finally:
        await db.close()


async def init_db(db_path=None) -> None:
    async with connect(db_path) as db:
        await db.executescript(SCHEMA)
        cur = await db.execute("PRAGMA table_info(jobs)")
        have = {r["name"] for r in await cur.fetchall()}
        for col, decl in _LATE_COLUMNS.items():
            if col not in have:
                await db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
        cur = await db.execute(
            "SELECT id, title, description FROM jobs WHERE role IS NULL OR role = ''")
        for row in await cur.fetchall():
            await db.execute("UPDATE jobs SET role = ? WHERE id = ?",
                             [parsing.role_of(dict(row)), row["id"]])
        cur = await db.execute("PRAGMA table_info(projects)")
        have = {r["name"] for r in await cur.fetchall()}
        for col in ("run", "intro"):
            if col not in have:
                await db.execute(f"ALTER TABLE projects ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


async def add_jobs(rows: Iterable[dict[str, Any]], db_path=None) -> int:
    """Insert the rows that are new by (title, company). Returns how many were actually written.

    Row at a time on purpose: an uncommitted INSERT is visible to a later SELECT on the same
    connection, so the pass that rejects a duplicate already in the table rejects the second copy
    inside one batch too - no in-memory seen-set to keep in step with the DB.

    A row that IS already here is not a second job, it is the same posting still up - so it is moved to
    today's tab rather than copied, and its write-once facts travel with it. That is what keeps the day
    strip honest (a live posting belongs to today, not to the day it was first seen) and what makes a
    re-scrape safe: nothing already applied to or already written to is done twice, and a run on the new
    day picks up only the half the row still owes.
    """
    async with connect(db_path) as db:
        inserted = 0
        for row in rows:
            title = str(row.get("title") or "").strip()
            company = str(row.get("company") or "").strip()
            platform = str(row.get("platform") or "").strip().lower()
            if not title or not company or not row.get("job_url"):
                continue
            # Strict source mapping: a scraper that loses its own name must not have the row written
            # under whichever board the CHECK constraint happens to accept. INSERT OR IGNORE would
            # swallow the CHECK violation silently, so the refusal is made here, out loud.
            if platform not in config.PLATFORMS:
                print(f"add_jobs: refused {title!r} @ {company!r} - unknown platform {platform!r}")
                continue
            hit = await (await db.execute(_FINGERPRINT, [title, company])).fetchone()
            if hit:
                # Only when the day actually changes: a scrape run twice in an afternoon is not news,
                # and rewriting created_at would just churn the row.
                cur = await db.execute(
                    "UPDATE jobs SET created_at = datetime('now','localtime')"
                    " WHERE id = ? AND date(created_at) <> date('now','localtime')", [hit["id"]])
                if cur.rowcount:
                    print(f"add_jobs: {title!r} @ {company!r} is already queued - moved to today")
                continue
            if config.COMPANY_COOLDOWN_DAYS > 0 and await (await db.execute(
                    "SELECT 1 FROM company_history WHERE company_norm = ?"
                    " AND last_action_at > datetime('now','localtime', ?)",
                    [company.lower(), f"-{int(config.COMPANY_COOLDOWN_DAYS)} days"])).fetchone():
                print(f"add_jobs: skipped {title!r} @ {company!r} - approached within cooldown")
                continue
            clean = {**row, "title": title, "company": company, "platform": platform}
            # Derived here and not only by the scraper: every insert routes through this line, so a
            # caller that forgets cannot leave a row the dashboard's role filter is blind to.
            clean["role"] = clean.get("role") or parsing.role_of(clean)
            cur = await db.execute(_INSERT, [clean.get(c) or ("new" if c == "status" else None)
                                             for c in _COLUMNS])
            inserted += cur.rowcount or 0
        return inserted


async def remove_duplicates(db_path=None) -> int:
    """Drop every row sharing a (company, title) with a lower id, then make the flood impossible.

    Runs at boot because a jobs.db written before the fingerprint existed already holds duplicates.
    The UNIQUE index is created here and not in SCHEMA for exactly that reason: CREATE UNIQUE INDEX
    against a table that still holds duplicates fails outright, so the DELETE has to land first.

    ponytail: the index treats two genuinely distinct reqs with the same title at the same firm
    (same role posted per city) as one job. That is the point today - one application per role per
    company - and the upgrade path is to widen _DEDUPE_KEY with the city once locations are stored.
    """
    async with connect(db_path) as db:
        cur = await db.execute(
            f"DELETE FROM jobs WHERE id NOT IN (SELECT MIN(id) FROM jobs GROUP BY {_DEDUPE_KEY})")
        removed = cur.rowcount or 0
        await db.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs({_DEDUPE_KEY})")
    return removed


async def set_status(job_id: int, status: str | None, hr_email: str | None = None,
                     pdf_path: str | None = None, notes: str | None = None,
                     touch_applied_at: bool = True, template: str | None = None,
                     email_msgid: str | None = None, phase_fail: str | None = None,
                     db_path=None) -> None:
    """'applied'/'emailed' stamp applied_at server-side; others leave it untouched.

    hr_email/pdf_path/template/email_msgid use COALESCE so a later status change never wipes a known
    value. notes does NOT: it is this run's verdict, and the freshest one is the only one worth keeping.

    touch_applied_at=False suppresses the applied_at stamp: the pipeline owns applied_at through
    mark_submitted (the write-once submit fact), so its terminal status write must not re-stamp it -
    a row that only reached a human by email is 'emailed' with applied_at still NULL, still retryable.

    Reaching 'applied'/'emailed' also stamps company_history (write-once cooldown ledger, outside the
    daily-wiped jobs table) so tomorrow's scrape drops fresh reqs from a firm already approached.

    status=None writes the facts and no verdict at all, which is a state the app genuinely has: a run
    the operator closed mid-application still owes the recruiter a mail, and after sending it there is
    an address and a thread anchor to keep and still no honest label - 'emailed' would claim this got
    as far as a submitted application (it did not), and 'failed' would claim the outreach went nowhere
    (it went). The row keeps whatever verdict it already had, and what makes it visible is the facts:
    outreach_at set, applied_at not.
    """
    if status is not None and status not in config.JOB_STATUSES:
        raise ValueError(f"bad status: {status}")
    sets, params = ([], []) if status is None else (["status = ?"], [status])
    for col, val in (("hr_email", hr_email), ("pdf_path", pdf_path),
                     ("template", template), ("email_msgid", email_msgid)):
        if val is not None:
            sets.append(f"{col} = COALESCE(?, {col})")
            params.append(val)
    if notes is not None:
        sets.append("notes = ?")
        params.append(notes[:600])       # ponytail: one verdict per row, no history table until asked
    if phase_fail is not None:
        # Like notes: the freshest run's answer replaces the old one, and "" is how a run that landed
        # both halves clears a red chip. None (the default) leaves whatever is there untouched, which is
        # what the fact-only writes - the closing mail, a hand-stamp - want.
        sets.append("phase_fail = ?")
        params.append(",".join(sorted({p for p in str(phase_fail).split(",") if p}))[:120])
    if touch_applied_at and status in ("applied", "emailed"):
        sets.append("applied_at = datetime('now', 'localtime')")
    if not sets:
        return
    async with connect(db_path) as db:
        await db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", [*params, job_id])
        if status in ("applied", "emailed"):
            await db.execute(
                "INSERT INTO company_history (company_norm, last_action_at, kind)"
                " SELECT LOWER(TRIM(company)), datetime('now','localtime'), ? FROM jobs WHERE id = ?"
                " ON CONFLICT(company_norm) DO UPDATE SET last_action_at = excluded.last_action_at,"
                " kind = excluded.kind", [status, job_id])


async def set_session_id(job_id: int, cli_session_id: str, db_path=None) -> None:
    """Persist the Claude CLI session for this job so a retry resumes its context.

    Written straight after generation, before the terminal status: a job that fails later still
    carries a resumable session. `get`/`fetch` already SELECT *, so reading it back needs no getter.
    """
    async with connect(db_path) as db:
        await db.execute("UPDATE jobs SET cli_session_id = ? WHERE id = ?", [cli_session_id, job_id])


async def set_assets(job_id: int, assets_json: str, db_path=None) -> None:
    """Keep the generated copy for this job so the next run does not generate it a second time.

    Same shape as set_session_id: written as soon as the work is done, long before the terminal
    status, so a job the operator interrupts after the resume exists still has it on disk next time.
    """
    async with connect(db_path) as db:
        await db.execute("UPDATE jobs SET assets_json = ? WHERE id = ?", [assets_json, job_id])


async def get_cli_session(name: str = CLAUDE_SESSION, db_path=None) -> tuple[str | None, int]:
    """Return (cli_uuid, turns) for a named session, creating the row on first call.

    None means "no live conversation yet" - the caller starts one without --resume and calls
    set_cli_session with whatever UUID the CLI mints. Turns is only for logging and for deciding
    when the transcript has grown long enough to be worth recycling.
    """
    async with connect(db_path) as db:
        await db.execute("INSERT OR IGNORE INTO cli_sessions (name) VALUES (?)", [name])
        cur = await db.execute("SELECT session_id, turns FROM cli_sessions WHERE name = ?", [name])
        row = await cur.fetchone()
        return (row["session_id"] or None), row["turns"]


async def set_cli_session(session_id: str, name: str = CLAUDE_SESSION, db_path=None) -> None:
    """Bind a CLI UUID to the named session and count the turn."""
    async with connect(db_path) as db:
        await db.execute(
            "INSERT INTO cli_sessions (name, session_id, turns, updated_at)"
            " VALUES (?, ?, 1, datetime('now','localtime'))"
            " ON CONFLICT(name) DO UPDATE SET session_id = excluded.session_id,"
            " turns = cli_sessions.turns + 1, updated_at = excluded.updated_at",
            [name, session_id])


async def reset_cli_session(name: str = CLAUDE_SESSION, db_path=None) -> None:
    """Forget the UUID so the next call seeds a fresh conversation.

    Called when --resume is refused (transcript expired, context exhausted, another machine's UUID)
    - a dead session id must never wedge the pipeline into a permanent failure loop.
    """
    async with connect(db_path) as db:
        await db.execute("UPDATE cli_sessions SET session_id = NULL, turns = 0,"
                         " updated_at = datetime('now','localtime') WHERE name = ?", [name])


def _vault_key(domain: str) -> str:
    """Normalise a vault key so one employer can never occupy two rows.

    Read and write go through the same funnel: case, a stray '@' prefix and a 'www.' label are all
    noise that would otherwise split the cache and burn a second Hunter credit on a company already
    resolved. The caller decides what the key *means* (automations._company_key passes the company's
    core name, because the mail domain is unknown until an address is found).
    """
    return str(domain or "").strip().lower().lstrip("@").removeprefix("www.")


async def get_vault_email(domain: str, db_path=None) -> str | None:
    """The cached HR mailbox for this employer, or None. Free: no API call, no browser."""
    key = _vault_key(domain)
    if not key:
        return None
    async with connect(db_path) as db:
        cur = await db.execute("SELECT email FROM email_vault WHERE domain = ?", [key])
        row = await cur.fetchone()
        return (row["email"] or None) if row else None


async def save_vault_email(domain: str, email: str, source: str = "", db_path=None) -> bool:
    """Cache a discovered mailbox forever. Returns whether anything was written.

    Upsert rather than INSERT OR IGNORE: a later tier can find a better address for the same employer
    (a named recruiter where the first pass only had a generic careers@), and the newest verified hit
    is the one worth keeping. Blank key or blank address writes nothing - a cached empty string would
    poison every future lookup for that company.
    """
    key, addr = _vault_key(domain), str(email or "").strip().lower()
    if not (key and addr):
        return False
    async with connect(db_path) as db:
        await db.execute(
            "INSERT INTO email_vault (domain, email, source, discovered_at)"
            " VALUES (?, ?, ?, datetime('now','localtime'))"
            " ON CONFLICT(domain) DO UPDATE SET email = excluded.email, source = excluded.source,"
            " discovered_at = excluded.discovered_at", [key, addr, source or None])
    return True


async def vault_size(db_path=None) -> int:
    async with connect(db_path) as db:
        return (await (await db.execute("SELECT COUNT(*) FROM email_vault")).fetchone())[0]


async def mark_submitted(job_id: int, db_path=None) -> None:
    """Stamp applied_at the moment a platform accepts the application, before any outreach runs.

    An application is irreversible: it is in the employer's ATS the second the submit lands. The
    terminal status is NOT proof of it - a job that submits and then finds no mailbox and no recruiter
    is written 'failed', and re-queueing that row would apply to the same employer twice. applied_at is
    the fact ('we submitted'), status is the verdict ('both channels worked'). Keep them separate.
    """
    async with connect(db_path) as db:
        await db.execute(
            "UPDATE jobs SET applied_at = COALESCE(applied_at, datetime('now','localtime'))"
            " WHERE id = ?", [job_id])


async def mark_reached(job_id: int, db_path=None) -> None:
    """Stamp outreach_at the moment a mail or a DM actually goes out.

    The mirror of mark_submitted, and needed for the same reason: a job whose application failed but
    whose email landed is written 'failed', so the retry runs the whole pipeline again and mails that
    recruiter a second time. Measured: two rounds reached the same five people before this existed.
    A send is as irreversible as a submit; outreach_at is the fact, status stays the verdict.
    """
    async with connect(db_path) as db:
        await db.execute(
            "UPDATE jobs SET outreach_at = COALESCE(outreach_at, datetime('now','localtime'))"
            " WHERE id = ?", [job_id])


async def mark_replied(job_id: int, db_path=None) -> None:
    """Write-once replied_at the first time a recruiter answers. A reply is a fact like a send.

    COALESCE so a later poll that re-sees the same thread never moves the stamp. 'replied' is a
    derived badge (replied_at IS NOT NULL), never a status - the baked CHECK on status is not widened.
    """
    async with connect(db_path) as db:
        await db.execute(
            "UPDATE jobs SET replied_at = COALESCE(replied_at, datetime('now','localtime'))"
            " WHERE id = ?", [job_id])


async def mark_followed(job_id: int, db_path=None) -> None:
    """Write-once followup_at the first (and only) nudge that goes out. One follow-up per row, ever."""
    async with connect(db_path) as db:
        await db.execute(
            "UPDATE jobs SET followup_at = COALESCE(followup_at, datetime('now','localtime'))"
            " WHERE id = ?", [job_id])


async def applied_recently(company: str, days: int, db_path=None) -> bool:
    """True if this employer was applied to / emailed within `days` - the cross-run cooldown gate.

    Reads company_history (survives the daily wipe), matched on the same LOWER(TRIM(company)) key the
    dedupe index uses. days<=0 disables the gate.
    """
    if days <= 0:
        return False
    async with connect(db_path) as db:
        row = await (await db.execute(
            "SELECT 1 FROM company_history WHERE company_norm = ?"
            " AND last_action_at > datetime('now','localtime', ?)",
            [str(company or "").strip().lower(), f"-{int(days)} days"])).fetchone()
        return row is not None


async def known_jobs(db_path=None) -> tuple[set[tuple[str, str]], set[str]]:
    """What `add_jobs` is going to refuse, asked in advance: the (title, company) pairs already in the
    queue and the companies still inside their cooldown.

    The scrape asks this before it spends a query. Without it a sweep stops the moment it has seen its
    quota, and the rows it already had are dropped at insert time - so "find 50" quietly became "write
    34", and the missing 16 were counted as if the boards had run dry. With it, the sweep keeps hunting
    until the quota is filled with jobs it does not already have.
    """
    async with connect(db_path) as db:
        cur = await db.execute("SELECT LOWER(TRIM(title)) AS t, LOWER(TRIM(company)) AS c FROM jobs")
        known = {(r["t"] or "", r["c"] or "") for r in await cur.fetchall()}
    cooled: set[str] = set()
    if config.COMPANY_COOLDOWN_DAYS > 0:
        async with connect(db_path) as db:
            cur = await db.execute(
                "SELECT company_norm FROM company_history WHERE last_action_at >"
                " datetime('now','localtime', ?)", [f"-{int(config.COMPANY_COOLDOWN_DAYS)} days"])
            cooled = {r["company_norm"] for r in await cur.fetchall()}
    return known, cooled


async def due_followups(days: int, db_path=None) -> Sequence[dict]:
    """Rows whose outreach has aged past `days` with no reply and no nudge yet.

    No mailbox is required, and requiring one is what made every LinkedIn-only row a dead entry in the
    follow-ups view: it was listed as awaiting a nudge and could never be nudged. A row reached over
    LinkedIn is nudged over LinkedIn, so it belongs on the same five-day clock as a mailed one.
    """
    async with connect(db_path) as db:
        cur = await db.execute(
            "SELECT * FROM jobs WHERE outreach_at IS NOT NULL"
            " AND replied_at IS NULL AND followup_at IS NULL"
            " AND outreach_at < datetime('now','localtime', ?) ORDER BY outreach_at ASC",
            [f"-{int(days)} days"])
        return [dict(r) for r in await cur.fetchall()]


async def record_sent(job_id: int, hr_email: str | None, msgid: str | None, db_path=None) -> None:
    """Attach the address and Message-ID a Sent-mailbox sweep proved. Status is deliberately untouched.

    Status is the pipeline's verdict, and a reconciled row may still owe its application - writing
    'emailed' here would retire the row before that application ever went out. Both columns COALESCE, so
    a value already on the row is never replaced by a later sweep.
    """
    async with connect(db_path) as db:
        await db.execute(
            "UPDATE jobs SET hr_email = COALESCE(hr_email, ?), email_msgid = COALESCE(email_msgid, ?)"
            " WHERE id = ?", [hr_email, msgid, job_id])


async def set_hr_email(job_id: int, email: str, db_path=None) -> str | None:
    """Write an operator-supplied mailbox onto the row, REPLACING whatever was there. Returns what was kept.

    set_status cannot do this job: its hr_email is COALESCE, which is right for every machine-written
    address and exactly wrong here - the human is typing an address *because* what the waterfall came
    back with (or failed to come back with) is why the row is red, so honouring the old value would
    keep the dud. Normalised the same way save_vault_email normalises, so the row and the vault cache
    of it cannot disagree about case. Blank and malformed input writes nothing and returns None: the
    caller reports that back rather than stamping an address nobody can mail.
    """
    addr = str(email or "").strip().lower()
    local, _, domain = addr.partition("@")
    # Shape only, and deliberately loose: a real recruiter address can be almost anything, so the test
    # is the four things that make an address unmailable - no local part, no domain, no dot in it, or a
    # second '@' - and nothing else. Blank writes nothing too.
    if not (local and "." in domain and "@" not in domain and " " not in addr):
        return None
    async with connect(db_path) as db:
        await db.execute("UPDATE jobs SET hr_email = ? WHERE id = ?", [addr, job_id])
    return addr


async def clear_asset(job_id: int, db_path=None) -> None:
    """Drop the rendered-resume pointer from a row whose PDF is gone.

    A stale pdf_path is worse than none: the dashboard renders it as a download that 404s, and a retry
    would try to attach a file that is not there. set_status cannot do this - its pdf_path is COALESCE,
    which is exactly right for every other caller and exactly wrong for this one.
    """
    async with connect(db_path) as db:
        await db.execute("UPDATE jobs SET pdf_path = NULL WHERE id = ?", [job_id])


async def outreached(db_path=None) -> Sequence[dict]:
    """Every row an email actually went out on (outreach_at stamped) - the reply poller's candidate set."""
    async with connect(db_path) as db:
        cur = await db.execute("SELECT * FROM jobs WHERE outreach_at IS NOT NULL")
        return [dict(r) for r in await cur.fetchall()]


async def applied_today(db_path=None) -> int:
    """Applications actually submitted today - counted from applied_at, never from status.

    MAX_DAILY_APPLICATIONS is a real-world throttle on how many employers get contacted, so it has to
    count submissions. Counting status IN ('applied','emailed') undercounts every job that submitted but
    reached nobody, which lets a long queue push real applications past the cap the user set.
    """
    async with connect(db_path) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM jobs WHERE date(applied_at) = date('now','localtime')")
        return (await cur.fetchone())[0]


async def quota_left(db_path=None) -> int:
    return max(0, config.MAX_DAILY_APPLICATIONS - await applied_today(db_path))


async def clear_all_jobs(db_path=None, attempts: int = 3) -> int:
    """Wipe the queue: every row that never left this machine. Returns the number of rows deleted.

    NOT `DELETE FROM jobs`. A row that was SUBMITTED (applied_at) or MAILED (outreach_at) is an
    irreversible fact - an application sits in that employer's ATS forever and a cold mail sits in a
    recruiter's inbox forever - so deleting it would not undo anything, it would only make the app
    forget and do it a second time. Those rows stay (they are the emailed / follow-ups / applied
    sections), and the daily quota stays spent with them: applied_today() counts applied_at rows, so a
    wipe hands back only the budget that was never really used.

    Concurrency: a background scrape can be mid-INSERT the millisecond this fires. PRAGMA
    busy_timeout in connect() already waits out that writer, so the retry here only covers the case
    where the whole batch takes longer than the timeout - a fresh connection per attempt, since a
    connection that raised mid-transaction is not worth reusing. Anything that is not a lock is
    re-raised untouched; the route turns it into a visible banner.

    ponytail: DELETE, not DROP - the schema and its additive migrations stay put, so the next
    scrape needs no init pass. sqlite_sequence is reset only when the table ends up genuinely empty,
    or ids would collide with the surviving rows. Generated PDFs in generated_resumes/ are left on
    disk; they are cheap and reproducible.

    email_vault and company_history are NOT touched. The vault is a permanent cache of mailboxes that
    each cost a paid credit or a human login to discover, and company_history is what stops the same
    employer being approached twice across the wipe. The statement below names `jobs` explicitly for
    that reason - never widen it to a table sweep.
    """
    for attempt in range(attempts):
        try:
            async with connect(db_path) as db:
                deleted = (await (await db.execute(
                    f"SELECT COUNT(*) FROM jobs WHERE NOT ({_LEFT_THE_MACHINE})")).fetchone())[0]
                await db.execute(f"DELETE FROM jobs WHERE NOT ({_LEFT_THE_MACHINE})")
                left = (await (await db.execute("SELECT COUNT(*) FROM jobs")).fetchone())[0]
                seq = await (await db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'")).fetchone()
                if seq and not left:               # absent until an AUTOINCREMENT table exists
                    await db.execute("DELETE FROM sqlite_sequence WHERE name = 'jobs'")
            return deleted
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            if attempt == attempts - 1:
                raise
            print(f"clear_all_jobs: {exc} - retry {attempt + 1}/{attempts - 1}")
            await asyncio.sleep(0.4 * (attempt + 1))
    raise AssertionError("unreachable")


async def delete_jobs(ids: Iterable[int], db_path=None) -> int:
    """Delete exactly these rows: the per-row trash button and the checkbox selection. Returns the count.

    Unlike clear_all_jobs this CAN remove an applied or mailed row, because the operator named it
    deliberately - unticking and deleting one bad scrape is the whole point of the control. What
    survives is company_history, so the same employer stays cooled down for COMPANY_COOLDOWN_DAYS and
    cannot be re-applied to just because its row was thrown away.
    """
    wanted = [int(i) for i in ids]
    if not wanted:
        return 0
    async with connect(db_path) as db:
        cur = await db.execute(
            f"DELETE FROM jobs WHERE id IN ({','.join('?' * len(wanted))})", wanted)
        return cur.rowcount or 0


_ORDER = {None: "id DESC", "id": "id DESC",
          "fit": "fit_score IS NULL, fit_score DESC, id DESC"}


async def fetch(status: str | None = None, limit: int = 200, order: str | None = None,
                view: str | None = None, scraped: str | None = None, role: str | None = None,
                db_path=None) -> Sequence[dict]:
    """Rows for the queue, filtered by status, a named view, one scrape day and/or one role family.

    `view` is a key of _VIEWS ('recent', 'followups') and never reaches SQL as text from the caller -
    only the whitelisted predicate does. `scraped` is the All tab's date filter: an ISO day, checked by
    the route and then BOUND, never pasted - so an arbitrary string cannot become SQL here either, it
    just matches nothing. `role` is the same: a key of config.ROLES or nothing. Note there is no view
    that hides rows: 'all' really is all, including the applied/emailed rows a wipe deliberately kept.
    """
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if view in _VIEWS:
        clauses.append(_VIEWS[view])
    if scraped:
        clauses.append("date(created_at) = ?")
        params.append(scraped)
    if role:
        clauses.append("role = ?")
        params.append(role)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    async with connect(db_path) as db:
        cur = await db.execute(
            f"SELECT * FROM jobs {where} ORDER BY {_ORDER[order]} LIMIT ?", [*params, limit]
        )
        return [dict(r) for r in await cur.fetchall()]


async def get(job_id: int, db_path=None) -> dict | None:
    async with connect(db_path) as db:
        cur = await db.execute("SELECT * FROM jobs WHERE id = ?", [job_id])
        row = await cur.fetchone()
        return dict(row) if row else None


async def stats(db_path=None) -> dict[str, int]:
    """Status counts for the filter chips, plus a count per named view and per role family."""
    async with connect(db_path) as db:
        cur = await db.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status")
        out = {r["status"]: r["c"] for r in await cur.fetchall()}
        for name, clause in _VIEWS.items():
            out[name] = (await (await db.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {clause}")).fetchone())[0]
        for role in config.ROLES:
            out[role] = (await (await db.execute(
                "SELECT COUNT(*) FROM jobs WHERE role = ?", [role])).fetchone())[0]
        out["total"] = (await (await db.execute("SELECT COUNT(*) FROM jobs")).fetchone())[0]
        return out


async def scrape_days(db_path=None) -> Sequence[dict]:
    """Every scrape day still in the table, newest first, with how many rows it brought in.

    The All tab's date filter: not just the last six days, because the rows that left the machine
    outlive their scrape day (an application is kept until the reply window shuts, and a replied row
    forever), so the days worth offering are whatever is actually in the table. The caller turns each
    day into "N days old" - said in one place, next to the tabs that count the same thing.

    'untouched' is the number that decides whether an old day tab still earns a run-all: rows with no
    application and no outreach on them, still sitting at status 'queued' - exactly the rows such a run
    would start on. Anything the machine already did is not work, and neither is a row it rejected.
    """
    async with connect(db_path) as db:
        cur = await db.execute(
            f"SELECT {_SCRAPED_ON} day, COUNT(*) n,"
            " SUM(CASE WHEN status = 'queued' AND applied_at IS NULL AND outreach_at IS NULL"
            " THEN 1 ELSE 0 END) untouched"
            " FROM jobs GROUP BY day ORDER BY day DESC")
        return [dict(r) for r in await cur.fetchall()]


async def aged_jobs(scraped_days: int, contacted_days: int, db_path=None) -> Sequence[dict]:
    """The rows a retention clock has run out on: (id, company, created_at, applied_at, outreach_at) - the
    columns the purge needs to say when the row was scraped or sent, and nothing else.

    Two clocks, for the two kinds of row described in config, and both are counted from the day the row
    was scraped or sent as day one: a row that never reached anyone is scraped litter from the morning it
    would turn scraped_days, and a row that reached a human is a live thread that keeps the whole reply
    window - contacted_days from the SEND (outreach, or the application when no mail went out) - before it
    is retired. A reply stops both clocks dead: replied_at is the recruiter answering, and a row with an
    answer on it is the one thing here that must never be swept up.

    Returns the rows rather than deleting them, because what happens to the mailbox cache next depends
    on who else is still at that employer - a decision this layer has no business making.
    """
    sql = f"""SELECT id, company, created_at, applied_at, outreach_at FROM jobs
               WHERE replied_at IS NULL
                 AND ((applied_at IS NULL AND outreach_at IS NULL
                       AND {_SCRAPED_ON} <= date('now','localtime',?))
                      OR (COALESCE(outreach_at, applied_at) IS NOT NULL
                          AND date(COALESCE(outreach_at, applied_at)) <= date('now','localtime',?)))"""
    async with connect(db_path) as db:
        cur = await db.execute(sql, [f"-{int(scraped_days)} days", f"-{int(contacted_days)} days"])
        return [dict(r) for r in await cur.fetchall()]


async def companies(db_path=None) -> Sequence[str]:
    """Every employer still in the queue. The purge asks this before dropping a cached mailbox."""
    async with connect(db_path) as db:
        cur = await db.execute("SELECT company FROM jobs")
        return [r["company"] for r in await cur.fetchall()]


async def delete_vault_email(domain: str, db_path=None) -> bool:
    """Drop one cached mailbox from the vault. Returns whether a row went.

    The one thing in the app that removes from email_vault, and it only ever runs for an employer whose
    last row was just retired with no reply on it: the address was a guess at a company nobody is
    talking to any more. Everything else keeps the cache - it cost a paid credit or a human login, and
    clear_all_jobs deliberately cannot touch this table.
    """
    key = _vault_key(domain)
    if not key:
        return False
    async with connect(db_path) as db:
        cur = await db.execute("DELETE FROM email_vault WHERE domain = ?", [key])
        return bool(cur.rowcount)


async def unreached(db_path=None) -> Sequence[dict]:
    """Rows that were started and never recorded as reaching anyone - the app's "nothing was sent" claim.

    That claim can be wrong: SMTP accepts the message and the process dies before mark_reached writes
    the fact, or the row's status is later overwritten 'failed' because the APPLY half broke while the
    mail had already landed. This is the candidate set for reconciling the claim against the real Sent
    mailbox, which is the only way to stop a retry mailing that recruiter a second time.

    Two ways in, because a killed run leaves one of two shapes. A mailbox on the row is the obvious one.
    The other is a row with a cli_session_id and no outreach fact at all - that column is written by the
    AI stage as soon as a run picks the row up, so it is the only local evidence that the row was ever
    worked on. Measured live on job 23: killed after the mail went out, the row kept its cli_session_id
    and lost everything else, so an hr_email-only candidate set never offered it for reconciliation and
    the next batch mailed the same recruiter again. Plain queued rows that no run has touched carry a
    NULL session id and stay out - nothing was attempted, so nothing can be owed.
    """
    async with connect(db_path) as db:
        cur = await db.execute(
            "SELECT * FROM jobs WHERE outreach_at IS NULL"
            " AND (hr_email IS NOT NULL OR cli_session_id IS NOT NULL)")
        return [dict(r) for r in await cur.fetchall()]


async def response_rates(db_path=None) -> dict:
    """Reply-rate breakdown per platform / track / template, plus the send-to-reply funnel.

    'sent' counts rows an email went out on (outreach_at); 'replied' counts those that came back
    (replied_at). rate is replied/sent in [0,1], 0 when nothing was sent. NULL group keys are folded
    into '-' so a row scraped before track/template existed still tallies somewhere.
    """
    def _bars(rows):
        out = []
        for r in rows:
            sent = r["sent"] or 0
            replied = r["replied"] or 0
            out.append({"key": r["key"] or "-", "sent": sent, "replied": replied,
                        "rate": round(replied / sent, 3) if sent else 0.0})
        return sorted(out, key=lambda x: x["sent"], reverse=True)

    async with connect(db_path) as db:
        groups = {}
        for dim in ("platform", "track", "template"):
            cur = await db.execute(
                f"SELECT {dim} key, COUNT(outreach_at) sent,"
                " SUM(CASE WHEN replied_at IS NOT NULL THEN 1 ELSE 0 END) replied"
                " FROM jobs WHERE outreach_at IS NOT NULL GROUP BY key")
            groups[dim] = _bars(await cur.fetchall())
        funnel_row = await (await db.execute(
            "SELECT SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) queued,"
            " COUNT(applied_at) applied, COUNT(outreach_at) emailed,"
            " COUNT(replied_at) replied, COUNT(followup_at) followed FROM jobs")).fetchone()
        funnel = {k: (funnel_row[k] or 0) for k in ("queued", "applied", "emailed",
                                                    "replied", "followed")}
    return {**groups, "funnel": funnel}


async def daily_posts(limit: int = 30, db_path=None) -> Sequence[dict]:
    """The most recent daily builds and profile refreshes, newest day first.

    One read serves everything the dashboard and social.py need from this table: today's row (the
    once-a-day gate), the history panel, and the last profile refresh, which is the 15-day clock.
    """
    async with connect(db_path) as db:
        cur = await db.execute(
            "SELECT * FROM daily_posts ORDER BY day DESC, kind LIMIT ?", [limit])
        return [dict(r) for r in await cur.fetchall()]


async def save_daily_post(day: str, kind: str, *, status: str, title: str = "", summary: str = "",
                          slug: str = "", body: str = "", repo_name: str = "", repo_url: str = "",
                          image_path: str = "", error: str = "", db_path=None) -> None:
    """Record one day's build or profile refresh, replacing any earlier row for the same (day, kind).

    Upsert, not insert: a retry after a failure is the same day and the same kind, and it must leave
    one row carrying the latest truth rather than a history of the attempts.
    """
    async with connect(db_path) as db:
        await db.execute(
            "INSERT INTO daily_posts"
            " (day, kind, title, summary, slug, body, repo_name, repo_url, image_path, status, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(day, kind) DO UPDATE SET"
            " title=excluded.title, summary=excluded.summary, slug=excluded.slug, body=excluded.body,"
            " repo_name=excluded.repo_name, repo_url=excluded.repo_url,"
            " image_path=excluded.image_path, status=excluded.status, error=excluded.error,"
            " posted_at=datetime('now','localtime')",
            [day, kind, title, summary, slug, body, repo_name, repo_url, image_path, status, error])


_PROJECT_FIELDS = ("title", "pitch", "tech", "run", "intro", "phases", "done", "status", "announced",
                   "repo_url", "files", "output", "headline", "about", "finished_day")


async def active_project(db_path=None) -> dict | None:
    """The one project still being built, or None when the next press starts a new one."""
    async with connect(db_path) as db:
        cur = await db.execute(
            "SELECT * FROM projects WHERE status='building' ORDER BY started_day DESC LIMIT 1")
        row = await cur.fetchone()
        return dict(row) if row else None


async def open_project(slug: str, *, title: str, pitch: str, tech: str, phases: str,
                       run: str = "python main.py", intro: str = "", headline: str = "",
                       about: str = "", day: str, db_path=None) -> None:
    """The announcement's row. Started, not finished: every later press works on this one.

    Written BEFORE the repository exists, on purpose. If the push or the post is what fails, this row
    is what tells the next press that today's project is already chosen - without it, the retry would
    pick a new project and open a second repository for the same day.
    """
    async with connect(db_path) as db:
        await db.execute(
            "INSERT INTO projects (slug, title, pitch, tech, run, intro, phases, status, headline,"
            " about, started_day) VALUES (?,?,?,?,?,?,?,'building',?,?,?)"
            " ON CONFLICT(slug) DO UPDATE SET title=excluded.title, pitch=excluded.pitch,"
            " tech=excluded.tech, run=excluded.run, intro=excluded.intro, phases=excluded.phases,"
            " headline=excluded.headline, about=excluded.about",
            [slug, title, pitch, tech, run, intro, phases, headline, about, day])


async def update_project(slug: str, *, db_path=None, **fields) -> None:
    """Whatever the last phase learned about the project - its files, its proof, its progress."""
    sets = {k: v for k, v in fields.items() if k in _PROJECT_FIELDS}
    if not sets:
        return
    async with connect(db_path) as db:
        await db.execute(
            f"UPDATE projects SET {', '.join(f'{k}=?' for k in sets)} WHERE slug=?",
            [*sets.values(), slug])


async def project_history(limit: int = 20, db_path=None) -> Sequence[dict]:
    """Every project, newest first. The finished ones are what the GitHub profile links to."""
    async with connect(db_path) as db:
        cur = await db.execute(
            "SELECT * FROM projects ORDER BY started_day DESC LIMIT ?", [limit])
        return [dict(r) for r in await cur.fetchall()]


async def _selfcheck() -> None:
    import tempfile, os
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    await init_db(path)
    row = {"title": "SecOps Eng", "company": "Acme", "job_url": "http://x/1",
           "platform": "linkedin", "salary": "12 LPA", "bond_terms": "none",
           "recruiter_linkedin_url": "https://www.linkedin.com/in/probe-recruiter/",
           "date_posted": "2026-08-22"}
    assert await add_jobs([row], path) == 1
    assert await add_jobs([row], path) == 0, "same title+company must dedupe"
    assert await add_jobs([row | {"job_url": "http://x/1?refid=sponsored-9"}], path) == 0, \
        "a sponsored re-post under a fresh URL is still the same job"
    assert (await fetch(db_path=path))[0]["date_posted"] == "2026-08-22"
    assert (await fetch(db_path=path))[0]["recruiter_linkedin_url"].endswith("probe-recruiter/")
    assert await applied_today(path) == 0
    jid = (await fetch(db_path=path))[0]["id"]
    assert (await get(jid, path))["company"] == "Acme"
    assert await get(999_999, path) is None
    await set_status(jid, "applied", hr_email="hr@acme.com", pdf_path="x.pdf", db_path=path)
    assert await applied_today(path) == 1
    assert await quota_left(path) == config.MAX_DAILY_APPLICATIONS - 1
    await set_status(jid, "queued", db_path=path)
    queued = (await fetch("queued", db_path=path))[0]
    assert queued["hr_email"] == "hr@acme.com", "email must persist"
    assert queued["pdf_path"] == "x.pdf", "resume path must persist"
    assert queued["cli_session_id"] is None
    await set_session_id(jid, "7114026a-0dfe-4a79-b3eb-0dc6e590e243", path)
    await set_status(jid, "failed", notes="apply: stalled | outreach: no HR email", db_path=path)
    assert (await get(jid, path))["cli_session_id"] == "7114026a-0dfe-4a79-b3eb-0dc6e590e243", \
        "CLI session must survive a later status write"
    # The reason a row failed has to outlive the terminal that produced it, or diagnosing it means
    # re-running the job - which re-mails the recruiter. Latest verdict wins; the previous one is noise.
    assert (await get(jid, path))["notes"] == "apply: stalled | outreach: no HR email"
    await set_status(jid, "failed", db_path=path)
    assert (await get(jid, path))["notes"] == "apply: stalled | outreach: no HR email", \
        "a status write with no verdict must not erase the one on file"
    await set_status(jid, "failed", notes="apply: submitted", db_path=path)
    assert (await get(jid, path))["notes"] == "apply: submitted", "the freshest verdict is the useful one"

    # outreach_at mirrors applied_at: stamped once, never moved. Without the COALESCE a retry of a row
    # whose mail landed but whose apply failed rewrites the stamp and the pipeline mails that recruiter
    # again - measured twice before this column existed.
    assert (await get(jid, path))["outreach_at"] is None
    await mark_reached(jid, path)
    reached_at = (await get(jid, path))["outreach_at"]
    assert reached_at, "a send nobody recorded is a send that repeats"
    await mark_reached(jid, path)
    assert (await get(jid, path))["outreach_at"] == reached_at, "outreach_at must not move on a retry"
    await set_status(jid, "queued", db_path=path)
    assert (await get(jid, path))["outreach_at"] == reached_at, "a status write must not clear the fact"
    await set_status(jid, "failed", db_path=path)
    try:
        await set_status(jid, "bogus", db_path=path)
        raise AssertionError("bad status accepted")
    except ValueError:
        pass

    legacy = os.path.join(tempfile.mkdtemp(), "legacy.db")   # pre-step-4 schema must migrate additively
    async with connect(legacy) as db:
        await db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, company TEXT,"
                         " job_url TEXT UNIQUE, platform TEXT, salary TEXT, bond_terms TEXT,"
                         " status TEXT DEFAULT 'new', hr_email TEXT, applied_at TEXT)")
        # A row written before the role column existed, stamped as having left the machine so the wipe
        # further down still finds nothing to delete here.
        await db.execute("INSERT INTO jobs (title, company, job_url, platform, status, applied_at)"
                         " VALUES ('Junior DevOps Engineer', 'Legacyco', 'http://l/1', 'naukri',"
                         " 'applied', datetime('now','localtime'))")
    await init_db(legacy)
    assert [r["role"] for r in await fetch(role="devops", db_path=legacy)] == ["devops"], \
        "boot has to backfill the role of a row scraped before the column existed"
    assert await fetch(role="security", db_path=legacy) == [], "the filter must not match a sibling"
    async with connect(legacy) as db:
        cols = {r["name"] for r in await (await db.execute("PRAGMA table_info(jobs)")).fetchall()}
        tables = {r["name"] for r in await (await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
    assert {"description", "pdf_path", "cli_session_id", "date_posted",
            "recruiter_linkedin_url", "outreach_at", "email_msgid", "replied_at",
            "followup_at", "fit_score", "track", "template", "assets_json", "notes",
            "phase_fail", "role"} <= cols, cols
    assert "cli_sessions" in tables, tables
    assert "company_history" in tables, tables

    # Named session: absent -> None, bound -> readable, rebound -> counted, reset -> None again.
    assert await get_cli_session(db_path=path) == (None, 0)
    await set_cli_session("aaaa-1111", db_path=path)
    assert await get_cli_session(db_path=path) == ("aaaa-1111", 1)
    await set_cli_session("aaaa-1111", db_path=path)
    sid, turns = await get_cli_session(db_path=path)
    assert (sid, turns) == ("aaaa-1111", 2), (sid, turns)
    await reset_cli_session(db_path=path)
    assert await get_cli_session(db_path=path) == (None, 0), "reset must clear the dead UUID"
    assert await get_cli_session("other-session", db_path=path) == (None, 0), "names are independent"

    # Kill-switch. The wipe takes everything that never left the machine and NOTHING else: jid was
    # applied above, so it is the one row that must survive a wipe. Forgetting a submission is how the
    # same employer gets applied to a second time, and it is why the quota comes back only partly.
    await set_status(jid, "applied", db_path=path)
    assert await save_vault_email("AcmeLabs.com", "hr@acmelabs.com", "hunter", path) is True
    assert await applied_today(path) == 1 and await clear_all_jobs(path) == 0, \
        "the wipe deleted a row that had already been submitted"
    kept = await fetch(db_path=path)
    assert [r["id"] for r in kept] == [jid] and kept[0]["applied_at"], kept
    assert await applied_today(path) == 1, "the wipe handed back a budget that was really spent"
    assert await add_jobs([row], path) == 0, \
        "a company applied to today stays blocked across the wipe - company_history outlives it"
    # The per-row control does remove it, deliberately, and the employer stays cooled by the ledger.
    assert await delete_jobs([jid], path) == 1 and await fetch(db_path=path) == []
    assert await delete_jobs([], path) == 0, "an empty selection must not try to delete anything"
    fresh = row | {"company": "Freshco", "job_url": "http://x/fresh"}
    assert await add_jobs([fresh], path) == 1
    assert await clear_all_jobs(path) == 1, "an unsent row is exactly what the wipe deletes"
    assert await fetch(db_path=path) == [] and await applied_today(path) == 0
    assert await quota_left(path) == config.MAX_DAILY_APPLICATIONS, "quota returns with the unsent rows"
    assert await add_jobs([fresh], path) == 1, "the wiped url is insertable again"
    assert (await fetch(db_path=path))[0]["id"] == 1, "ids restart once the table is genuinely empty"
    assert await clear_all_jobs(legacy) == 0, "wiping an empty legacy db (no sqlite_sequence) is fine"


    # The vault is a permanent cache: the daily wipe must not touch it, and one employer must never
    # occupy two rows because of case, a 'www.' label or a stray '@'.
    assert await get_vault_email("acmelabs.com", path) == "hr@acmelabs.com", "the wipe ate the vault"
    assert await vault_size(path) == 1
    assert await get_vault_email("WWW.AcmeLabs.COM", path) == "hr@acmelabs.com"
    assert await get_vault_email("@acmelabs.com", path) == "hr@acmelabs.com"
    assert await get_vault_email("otherfirm.com", path) is None
    assert await get_vault_email("", path) is None and await get_vault_email(None, path) is None
    assert await save_vault_email("acmelabs.com", "talent@acmelabs.com", "osint", path) is True
    assert await get_vault_email("acmelabs.com", path) == "talent@acmelabs.com", "a better hit upserts"
    assert await vault_size(path) == 1, "the upsert must replace the row, not add one"
    assert await save_vault_email("acmelabs.com", "", "jd", path) is False, "a blank hit is not cached"
    assert await save_vault_email("", "hr@x.com", "jd", path) is False, "a blank key is not cached"
    assert await get_vault_email("acmelabs.com", path) == "talent@acmelabs.com", "…and nothing changed"
    async with connect(path) as db:
        vrow = await (await db.execute("SELECT * FROM email_vault")).fetchone()
    assert vrow["source"] == "osint" and vrow["discovered_at"], dict(vrow)
    async with connect(legacy) as db:                  # additive on a pre-vault jobs.db
        tables = {r["name"] for r in await (await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
    assert "email_vault" in tables, tables

    # The duplicate flood, end to end. A pre-fingerprint db is simulated with raw INSERTs, because
    # that is exactly what the old job_url-keyed add_jobs did with 12 sponsored copies of one req.
    dupes = os.path.join(tempfile.mkdtemp(), "dupes.db")
    await init_db(dupes)
    async with connect(dupes) as db:
        await db.executemany(
            "INSERT INTO jobs (title, company, job_url, platform) VALUES (?,?,?,?)",
            [("SecOps Eng", "Accenture", f"http://y/{i}?refid=sponsored-{i}", "naukri")
             for i in range(12)])
    assert len(await fetch(db_path=dupes)) == 12
    assert await remove_duplicates(dupes) == 11, "keep only the lowest id per (company, title)"
    kept = await fetch(db_path=dupes)
    assert len(kept) == 1 and kept[0]["job_url"].endswith("sponsored-0"), "the oldest row survives"
    assert await remove_duplicates(dupes) == 0, "a second pass has nothing left to sweep"

    # With the index in place the flood cannot come back, and neither can it get in inside one batch.
    accenture = {**row, "title": "SecOps Eng", "company": "  ACCENTURE "}
    assert await add_jobs([accenture | {"job_url": f"http://z/{i}"} for i in range(12)], dupes) == 0
    assert await add_jobs([row | {"job_url": f"http://z/acme/{i}"} for i in range(12)], dupes) == 1, \
        "12 copies inside ONE batch collapse to a single row"
    assert await add_jobs([row | {"job_url": "http://z/bad", "platform": "monster"}], dupes) == 0, \
        "an unknown platform is refused, never rewritten as some other board"
    assert await add_jobs([row | {"job_url": "http://z/blank", "company": " "}], dupes) == 0
    assert len(await fetch(db_path=dupes)) == 2, await stats(dupes)
    assert {j["platform"] for j in await fetch(db_path=dupes)} == {"naukri", "linkedin"}

    # Lock safety. First: busy_timeout is actually set on every connection, not just asked for.
    async with connect(path) as db:
        assert (await (await db.execute("PRAGMA busy_timeout")).fetchone())[0] == 5000

    # Then: a real writer holding the lock. A second sqlite3 connection opens an exclusive transaction
    # and never commits, so the wipe cannot get the write lock and every attempt raises "locked". The
    # retry must exhaust and re-raise rather than hang forever or swallow the failure.
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        try:
            await clear_all_jobs(path, attempts=2)
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc).lower(), exc
        else:
            raise AssertionError("a wipe against an exclusive lock must raise, not silently pass")
    finally:
        blocker.rollback()
        blocker.close()
    assert await clear_all_jobs(path) == 1, "the wipe succeeds once the lock is released"

    # A non-lock OperationalError is re-raised untouched, not retried into a confusing timeout.
    try:
        await clear_all_jobs(config.BASE_DIR / "nope" / "missing.db")
    except sqlite3.OperationalError as exc:
        assert "locked" not in str(exc).lower()

    # Reply / follow-up are write-once FACTS, not statuses; company_history is a cross-run ledger that
    # outlives the daily wipe; response_rates rolls the send->reply funnel per platform/track/template.
    feat = os.path.join(tempfile.mkdtemp(), "feat.db")
    await init_db(feat)
    await add_jobs([{"title": "SOC Analyst", "company": "Zeta", "job_url": "http://f/1",
                     "platform": "naukri", "fit_score": 0.75, "track": "cybersecurity"}], feat)
    fid = (await fetch(db_path=feat))[0]["id"]
    assert (await get(fid, feat))["fit_score"] == 0.75 and (await get(fid, feat))["track"] == "cybersecurity"
    # The role family, derived at insert because not every caller knows it (the scraper does, a fixture
    # or a repair script does not) and the dashboard filter reads it back out of the table.
    assert (await get(fid, feat))["role"] == "security", "the title names the family"
    assert [r["id"] for r in await fetch(role="security", db_path=feat)] == [fid]
    assert await fetch(role="bogus", db_path=feat) == [], "a family that is not one matches nothing"

    await add_jobs([{"title": "Cloud Assoc", "company": "Yotta", "job_url": "http://f/2",
                     "platform": "linkedin", "fit_score": 0.9, "track": "cloud", "status": "queued",
                     "hr_email": "hr@yotta.com"}], feat)
    await set_status(fid, "queued", db_path=feat)
    ordered = await fetch("queued", order="fit", db_path=feat)
    assert [r["fit_score"] for r in ordered] == [0.9, 0.75], ordered
    assert (await get(fid + 1, feat))["role"] == "devops"
    assert [r["id"] for r in await fetch("queued", role="devops", db_path=feat)] == [fid + 1], \
        "the family filter composes with the status filter instead of replacing it"
    counts = await stats(feat)
    assert (counts["security"], counts["devops"], counts["software"]) == (1, 1, 0), counts

    await set_status(fid, "emailed", hr_email="hr@zeta.com", template="cdac",
                     email_msgid="<abc@host>", db_path=feat)
    assert (await get(fid, feat))["replied_at"] is None
    assert (await get(fid, feat))["template"] == "cdac"
    assert (await get(fid, feat))["email_msgid"] == "<abc@host>"
    await mark_reached(fid, feat)
    # Views and the send-reconciliation candidate set. The day strip is the SCRAPE DAY, so the same row
    # walks the tabs as it ages: in 'recent' while it is today's haul, in 'd3' once it is three days old,
    # and in no day tab at all past the retention window - which is exactly when aged_jobs picks it up.
    # 'recent' is today and NOT "the newest day in the table": with only old rows in the fixture it has
    # to read empty, because that emptiness is the answer to the one question the tab exists for - did
    # today's scrape run? 'followups' is mailed and unanswered; unreached() is the set whose "nothing was
    # sent" claim the Sent mailbox re-checks.
    assert [r["id"] for r in await fetch(view="followups", db_path=feat)] == [fid], \
        "a mailed, unanswered row is exactly the follow-ups view"
    assert fid in {r["id"] for r in await fetch(view="recent", db_path=feat)}
    assert {r["id"] for r in await unreached(feat)} == {fid + 1}, "a reached row is not a send candidate"
    async with connect(feat) as db:
        await db.execute("UPDATE jobs SET created_at = datetime('now','localtime','-3 days')"
                         " WHERE id = ?", [fid])
    assert fid not in {r["id"] for r in await fetch(view="recent", db_path=feat)}, \
        "a three-day-old row is not today's scrape"
    assert fid in {r["id"] for r in await fetch(view="d3", db_path=feat)}, \
        "three days old is the -3d tab, not the recent one"
    assert await fetch(view="d2", db_path=feat) == [] and await fetch(view="d5", db_path=feat) == [], \
        "a day tab is that day and no other"
    # The All tab's picker: every day still in the table (newest first, each one once) and the bound
    # parameter the fetch filters on. A day with no rows is empty rather than everything.
    span = [d["day"] for d in await scrape_days(db_path=feat)]
    assert span == sorted(span, reverse=True) and len(set(span)) == len(span), span
    assert [len(await fetch(scraped=d, db_path=feat)) for d in span] == [1, 1], span
    assert {r["id"] for r in await fetch(scraped=span[0], db_path=feat)} == {fid + 1}, \
        "the picker's day does not select that scrape day"
    assert await fetch(scraped="1999-01-01", db_path=feat) == [], \
        "a day nothing was scraped on must be empty, not everything"
    # Retention, the two clocks, counted with the day it was scraped as day one. fid is three days old
    # and was reached TODAY, so neither has run out: the scrape clock because this row left the machine,
    # the contact clock because the send is hours old. A row nobody was ever told about is the only thing
    # the five-day clock takes.
    assert await aged_jobs(5, 10, feat) == [], "a freshly reached row is nobody's litter"
    async with connect(feat) as db:
        await db.execute("UPDATE jobs SET created_at = datetime('now','localtime','-5 days')"
                         " WHERE id = ?", [fid + 1])
    assert [r["id"] for r in await aged_jobs(5, 10, feat)] == [fid + 1], \
        "a row nobody was told about is litter on the morning it turns five days old"
    assert await aged_jobs(6, 10, feat) == [], \
        "the window is the argument, not a number baked in here"
    async with connect(feat) as db:
        await db.execute("UPDATE jobs SET outreach_at = datetime('now','localtime','-11 days')"
                         " WHERE id = ?", [fid])
    assert fid in {r["id"] for r in await aged_jobs(5, 10, feat)}, \
        "eleven days of silence is the other clock running out"
    assert await fetch(view="followups", db_path=feat, limit=0) == [], \
        "a view must compose with the ordinary LIMIT, not replace it"
    await mark_replied(fid, feat)
    assert fid not in {r["id"] for r in await aged_jobs(5, 10, feat)}, \
        "a reply stops both clocks - the recruiter is the one row that is never swept"
    first_reply = (await get(fid, feat))["replied_at"]
    assert first_reply, "a reply nobody recorded is a reply that never trips the follow-up skip"
    await mark_replied(fid, feat)
    assert (await get(fid, feat))["replied_at"] == first_reply, "replied_at must not move on a re-poll"

    assert fid not in {r["id"] for r in await due_followups(0, feat)}, "a replied row is never nudged"
    await mark_followed(fid, feat)
    ff = (await get(fid, feat))["followup_at"]
    await mark_followed(fid, feat)
    assert (await get(fid, feat))["followup_at"] == ff, "one nudge per row, ever"

    # The count the run-all rule reads off each day, and the scrape that meets its own rows again.
    # 'untouched' is what a batch would actually start on: queued, never applied to, never written to -
    # so the moment a row is reached it stops counting, which is the whole point of the threshold.
    litter_day = (await get(fid + 1, feat))["created_at"][:10]
    counts = {d["day"]: d["untouched"] for d in await scrape_days(db_path=feat)}
    assert counts[litter_day] == 1, counts
    await mark_reached(fid + 1, feat)
    counts = {d["day"]: d["untouched"] for d in await scrape_days(db_path=feat)}
    assert counts[litter_day] == 0, "a row that was written to is not untouched work"
    # A scrape that meets a row it already has is not a second job, it is the same posting still up: the
    # row moves to today's tab carrying every fact on it, so nothing is applied to or mailed twice and a
    # run on the new day picks up only the half it still owes. The unique index is what makes a copy
    # impossible; this is what makes the meeting a move instead of a silent drop.
    await add_jobs([{"title": "SOC Analyst", "company": "Zeta", "job_url": "http://f/1",
                     "platform": "naukri", "status": "queued"}], feat)
    met = await get(fid, feat)
    assert met["outreach_at"] and met["status"] == "emailed" and met["replied_at"], \
        "a re-scrape rewrote the facts on the row it already had"
    assert fid in {r["id"] for r in await fetch(view="recent", db_path=feat)}, \
        "the row a scrape met again did not come back to today's tab"
    assert len(await fetch(db_path=feat)) == 2, "the scrape made a copy of a job it already had"

    rr = await response_rates(feat)
    assert set(rr) == {"platform", "track", "template", "funnel"}, rr
    for dim in ("platform", "track", "template"):
        for bar in rr[dim]:
            assert 0.0 <= bar["rate"] <= 1.0 and bar["replied"] <= bar["sent"], bar
    assert rr["funnel"]["emailed"] >= rr["funnel"]["replied"] >= 1, rr["funnel"]

    assert await applied_recently("zeta", config.COMPANY_COOLDOWN_DAYS, feat) is True
    assert await applied_recently("  ZETA ", 14, feat) is True, "norm matches the dedupe key"
    assert await applied_recently("neverheardof", 14, feat) is False
    assert await applied_recently("zeta", 0, feat) is False, "days<=0 disables the gate"
    before = len(await fetch(db_path=feat))
    assert await add_jobs([{"title": "New Role", "company": "Zeta", "job_url": "http://f/3",
                            "platform": "indeed"}], feat) == 0, "a cooled-down company is dropped"
    assert len(await fetch(db_path=feat)) == before
    await clear_all_jobs(feat)
    assert await applied_recently("zeta", 14, feat) is True, "the wipe must not touch company_history"

    await save_daily_post("2026-09-30", "announce", status="posted", title="Old Build", db_path=path)
    await save_daily_post("2026-10-01", "phase", status="failed", title="Draft", db_path=path)
    posts = await daily_posts(db_path=path)
    assert [p["day"] for p in posts] == ["2026-10-01", "2026-09-30"], posts
    await save_daily_post("2026-10-01", "phase", status="posted", title="Landed",
                          repo_url="https://github.com/x/y", db_path=path)
    posts = await daily_posts(db_path=path)
    assert len(posts) == 2 and posts[0]["title"] == "Landed" and posts[0]["status"] == "posted", \
        "a retry the same day must rewrite its row, not add a second one"
    await save_daily_post("2026-10-01", "profile", status="posted", title="Headline", db_path=path)
    assert len(await daily_posts(db_path=path)) == 3, "(day, kind) is the key, so profile is its own row"
    assert [p["kind"] for p in await daily_posts(db_path=path, limit=2)] == ["phase", "profile"]
    await clear_all_jobs(path)
    assert len(await daily_posts(db_path=path)) == 3, "the daily ledger is not part of the job queue"

    assert await active_project(path) is None, "a fresh database has no project in flight"
    await open_project("log-triage", title="Log Triage", pitch="Reads sshd logs.", tech="python",
                       phases='[{"name": "Parse", "what": "read the log"}, {"name": "Report", "what": "count"}]',
                       run="python main.py sample/sshd.log", intro="Building a log triager.",
                       headline="DevSecOps", about="I build tools.", day="2026-10-01", db_path=path)
    proj = await active_project(path)
    assert proj["slug"] == "log-triage" and proj["done"] == 0 and proj["announced"] == 0, proj
    assert proj["status"] == "building" and proj["files"] == "{}" and proj["repo_url"] == ""
    assert proj["run"] == "python main.py sample/sshd.log", proj["run"]
    assert proj["intro"] == "Building a log triager.", "the retry lane replays this, so it is stored"
    await update_project("log-triage", announced=1, done=1, repo_url="https://github.com/x/log-triage",
                         files='{"main.py": "abc"}', output="2 lines", db_path=path,
                         not_a_column="ignored")
    proj = await active_project(path)
    assert proj["announced"] == 1 and proj["done"] == 1 and proj["output"] == "2 lines", proj
    assert json.loads(proj["phases"])[1]["name"] == "Report", proj["phases"]
    await update_project("log-triage", status="finished", finished_day="2026-10-04", db_path=path)
    assert await active_project(path) is None, "a finished project must not block the next one"
    await open_project("iam-lint", title="IAM Lint", pitch="x", tech="python", phases="[]",
                       day="2026-10-05", db_path=path)
    history = await project_history(db_path=path)
    assert [p["slug"] for p in history] == ["iam-lint", "log-triage"], history
    assert await active_project(path) is not None, "the new project is the one in flight"

    print("database.py selfcheck ok", await stats(path))


if __name__ == "__main__":
    import asyncio
    asyncio.run(_selfcheck())
